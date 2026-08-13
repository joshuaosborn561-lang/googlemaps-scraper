"""LLM backends.

Two implementations of one interface (`check`, `json_chat`), so the stages do
not care which is configured:

* `OpenAICompat` — any OpenAI-shaped endpoint. Default is `gpt-5-nano`, which
  runs a national vertical for a couple of dollars and removes the local
  hardware problem entirely. Point `OPENAI_BASE_URL` elsewhere to use
  DeepSeek, LM Studio, GenieX's `geniex serve`, or anything else speaking the
  same protocol.
* `Ollama` — fully local, free, offline. Slow on CPU-only machines.

Both constrain output to a JSON schema rather than asking nicely and parsing:
Ollama via `format`, OpenAI via Structured Outputs with `strict: true`. That
guarantee is load-bearing -- a stage makes tens of thousands of unattended
calls, and a malformed response is a silently missing row in the CSV.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from typing import Any

import requests


class LLMError(RuntimeError):
    pass


class RateLimitError(LLMError):
    """OpenAI/provider 429 — caller must NOT persist a permanent failure."""


OllamaError = LLMError          # historical name, still imported by the stages

# Process-wide cap so classify + SERP extract cannot stampede one API key.
_llm_sema: threading.BoundedSemaphore | None = None
_llm_sema_n = 0
_llm_sema_lock = threading.Lock()


def llm_max_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("LLM_MAX_CONCURRENCY", "6")))
    except ValueError:
        return 6


def _llm_gate() -> threading.BoundedSemaphore:
    """Shared semaphore; rebuilt if LLM_MAX_CONCURRENCY changes at runtime."""
    global _llm_sema, _llm_sema_n
    n = llm_max_concurrency()
    with _llm_sema_lock:
        if _llm_sema is None or _llm_sema_n != n:
            _llm_sema = threading.BoundedSemaphore(n)
            _llm_sema_n = n
        return _llm_sema


def is_rate_limit_error(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text


def _metrics(body: dict[str, Any]) -> dict[str, float]:
    """Split Ollama's nanosecond timings into prefill vs generation rates."""
    ns = 1_000_000_000

    def rate(count_key: str, dur_key: str) -> float:
        n = body.get(count_key) or 0
        d = body.get(dur_key) or 0
        return (n / (d / ns)) if n and d else 0.0

    return {
        "prompt_tokens": float(body.get("prompt_eval_count") or 0),
        "output_tokens": float(body.get("eval_count") or 0),
        "prefill_tok_s": rate("prompt_eval_count", "prompt_eval_duration"),
        "gen_tok_s": rate("eval_count", "eval_duration"),
        "total_s": (body.get("total_duration") or 0) / ns,
        "load_s": (body.get("load_duration") or 0) / ns,
    }


def strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a JSON schema acceptable to OpenAI Structured Outputs.

    `strict: true` requires every object to forbid extra properties and to
    list all of its properties as required. Ours already require everything;
    this makes it explicit, and recursive.
    """
    if isinstance(schema, dict):
        out = {k: strictify(v) for k, v in schema.items()}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out
    if isinstance(schema, list):
        return [strictify(v) for v in schema]
    return schema


_PY_TYPES = {
    "string": str, "number": (int, float), "integer": int,
    "boolean": bool, "array": list, "object": dict, "null": type(None),
}


def _type_ok(value: Any, spec: dict[str, Any]) -> bool:
    declared = spec.get("type")
    if declared is None:
        return True
    for name in (declared if isinstance(declared, list) else [declared]):
        py = _PY_TYPES.get(name)
        if py is None:
            return True                      # unknown keyword: do not police it
        if name in ("number", "integer") and isinstance(value, bool):
            continue                         # bool is a subclass of int
        if isinstance(value, py):
            return True
    return False


def schema_errors(obj: Any, schema: dict[str, Any]) -> list[str]:
    """Cheap structural check on a parsed response.

    Not a full JSON Schema validator -- just enough to catch a backend that
    ignored the schema. Necessary because schema enforcement is a guarantee
    only on some endpoints: OpenRouter routes a model across several
    providers, and one that lacks native strict mode may silently downgrade
    to plain JSON mode. Over tens of thousands of unattended calls that shows
    up as missing CSV rows, not as an error.
    """
    if schema.get("type") != "object":
        return []
    if not isinstance(obj, dict):
        return [f"expected an object, got {type(obj).__name__}"]
    errs = []
    for key in schema.get("required") or []:
        if key not in obj:
            errs.append(f"missing '{key}'")
    for key, spec in (schema.get("properties") or {}).items():
        if key in obj and isinstance(spec, dict) and not _type_ok(obj[key], spec):
            errs.append(f"'{key}' has the wrong type")
    return errs


class _Base:
    """Shared token and cost accounting."""

    def __init__(self) -> None:
        self.tokens_in = 0
        self.tokens_out = 0
        self.calls = 0
        self.repairs = 0             # responses that failed the schema check
        self.price_in = 0.0          # USD per million tokens
        self.price_out = 0.0
        self.last: dict[str, float] = {}

    @property
    def cost_usd(self) -> float:
        return (
            self.tokens_in / 1e6 * self.price_in
            + self.tokens_out / 1e6 * self.price_out
        )

    def spend_line(self) -> str:
        if not self.calls:
            return ""
        repair = f", {self.repairs:,} schema repairs" if self.repairs else ""
        if self.price_in or self.price_out:
            return (
                f"  {self.calls:,} LLM calls, {self.tokens_in:,} in / "
                f"{self.tokens_out:,} out tokens, ~${self.cost_usd:,.2f}{repair}"
            )
        return f"  {self.calls:,} LLM calls (local, free){repair}"


class OpenAICompat(_Base):
    """Chat Completions against any OpenAI-shaped endpoint."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5-nano",
        base_url: str = "https://api.openai.com/v1",
        timeout: int = 120,
        max_retries: int = 5,
        price_in: float = 0.05,
        price_out: float = 0.40,
    ):
        super().__init__()
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.price_in = price_in
        self.price_out = price_out
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        )

    def check(self) -> None:
        if not self.api_key:
            raise SystemExit(
                "OPENAI_API_KEY is not set. Add it to .env, or switch to local "
                "inference with LLM_PROVIDER=ollama."
            )

    def json_chat(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "result",
                    "strict": True,
                    "schema": strictify(schema),
                },
            },
        }
        # Several small models reject an explicit temperature; only send a
        # non-default one.
        if temperature:
            payload["temperature"] = temperature

        # OpenRouter serves one model from several providers and only some
        # honour json_schema; without this it may route to one that quietly
        # downgrades to plain JSON mode.
        if "openrouter" in self.base_url:
            payload["provider"] = {"require_parameters": True}

        last: Exception | None = None
        saw_429 = False
        gate = _llm_gate()
        for attempt in range(self.max_retries + 1):
            try:
                with gate:
                    r = self.session.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        timeout=self.timeout,
                    )
                if r.status_code in (401, 403):
                    raise SystemExit(
                        f"HTTP {r.status_code} from {self.base_url}: check "
                        f"OPENAI_API_KEY. {r.text[:200]}"
                    )
                if r.status_code == 429 or r.status_code >= 500:
                    wait = float(r.headers.get("Retry-After") or 0) or (
                        2 ** (attempt + 1)
                    )
                    if r.status_code == 429:
                        saw_429 = True
                        # Back off hard — stampeding retries make throughput worse.
                        wait = max(wait, 15.0) * (1.0 + 0.25 * attempt)
                    last = (
                        RateLimitError("HTTP 429")
                        if r.status_code == 429
                        else LLMError(f"HTTP {r.status_code}")
                    )
                    time.sleep(min(wait, 90) + random.uniform(0, 1))
                    continue
                r.raise_for_status()
                body = r.json()

                usage = body.get("usage") or {}
                self.calls += 1
                self.tokens_in += int(usage.get("prompt_tokens") or 0)
                self.tokens_out += int(usage.get("completion_tokens") or 0)
                self.last = {
                    "prompt_tokens": float(usage.get("prompt_tokens") or 0),
                    "output_tokens": float(usage.get("completion_tokens") or 0),
                }

                choice = (body.get("choices") or [{}])[0]
                if choice.get("finish_reason") == "length":
                    raise LLMError("response truncated before the JSON closed")
                content = (choice.get("message") or {}).get("content") or ""
                if not content:
                    raise LLMError(f"empty response: {json.dumps(body)[:200]}")

                obj = json.loads(content)
                errs = schema_errors(obj, schema)
                if not errs:
                    return obj
                # The backend ignored the schema. Say so explicitly and retry
                # rather than writing a malformed row.
                self.repairs += 1
                payload["messages"] = payload["messages"][:2] + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content":
                        "That response did not match the required schema: "
                        + "; ".join(errs)
                        + ". Reply with ONLY the corrected JSON object."},
                ]
                raise LLMError("schema mismatch: " + "; ".join(errs))
            except SystemExit:
                raise
            except RateLimitError as exc:
                last = exc
                saw_429 = True
                if attempt < self.max_retries:
                    time.sleep(min(30 * (attempt + 1), 90) + random.uniform(0, 1))
            except (requests.RequestException, ValueError, LLMError) as exc:
                last = exc
                if is_rate_limit_error(exc):
                    saw_429 = True
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
        if saw_429 or isinstance(last, RateLimitError) or is_rate_limit_error(last or ""):
            raise RateLimitError(
                f"failed after {self.max_retries + 1} attempts: {last}"
            )
        raise LLMError(f"failed after {self.max_retries + 1} attempts: {last}")


class Ollama(_Base):
    """Local inference. Free and offline; slow without a GPU."""

    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = "gemma4:e4b",
        timeout: int = 600,
        keep_alive: str = "10m",
        num_ctx: int = 4096,
        num_threads: int = 0,
    ):
        super().__init__()
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self.num_threads = num_threads
        self.session = requests.Session()

    def check(self) -> None:
        """Fail early and with a fixable message rather than mid-run."""
        try:
            r = self.session.get(f"{self.host}/api/tags", timeout=10)
            r.raise_for_status()
            tags = [m["name"] for m in r.json().get("models", [])]
        except requests.RequestException as exc:
            raise SystemExit(
                f"Cannot reach Ollama at {self.host} ({exc}).\n"
                f"Start it with `ollama serve` and check OLLAMA_HOST in .env."
            ) from exc

        # Ollama reports "gemma4:e4b"; accept a bare "gemma4" as a match too.
        if not any(t == self.model or t.split(":")[0] == self.model.split(":")[0]
                   for t in tags):
            raise SystemExit(
                f"Model '{self.model}' is not pulled. Run:\n"
                f"    ollama pull {self.model}\n"
                f"Installed: {', '.join(tags) or '(none)'}"
            )

    def _options(self, temperature: float) -> dict[str, Any]:
        opts: dict[str, Any] = {"temperature": temperature, "num_ctx": self.num_ctx}
        if self.num_threads > 0:
            opts["num_thread"] = self.num_threads
        return opts

    def json_chat(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,
            "keep_alive": self.keep_alive,
            "options": self._options(temperature),
        }
        try:
            r = self.session.post(
                f"{self.host}/api/chat", json=payload, timeout=self.timeout
            )
            r.raise_for_status()
            body = r.json()
            content = body.get("message", {}).get("content", "")
            self.last = _metrics(body)
            self.calls += 1
            self.tokens_in += int(self.last["prompt_tokens"])
            self.tokens_out += int(self.last["output_tokens"])
        except requests.Timeout as exc:
            raise LLMError(
                f"ollama timed out after {self.timeout}s. On CPU-only hardware "
                f"try a smaller model (gemma4:e4b, qwen3.5:4b, phi4-mini) and a "
                f"shorter prompt -- run `python -m gmscraper bench`. Or switch "
                f"to LLM_PROVIDER=openai."
            ) from exc
        except requests.RequestException as exc:
            raise LLMError(f"ollama request failed: {exc}") from exc

        if not content:
            raise LLMError("empty response from model")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned non-JSON: {content[:200]}") from exc


def make_llm(
    settings,
    model: str = "",
    host: str = "",
    num_ctx: int = 0,
    num_threads: int = 0,
    provider: str = "",
):
    """Build the configured backend and verify it before any stage runs."""
    name = (provider or settings.llm_provider or "openai").lower()
    if name in ("openai", "cloud"):
        llm: _Base = OpenAICompat(
            api_key=settings.openai_api_key,
            model=model or settings.openai_model,
            base_url=settings.openai_base_url,
            price_in=settings.openai_price_in,
            price_out=settings.openai_price_out,
        )
    elif name == "ollama":
        llm = Ollama(
            host=host or settings.ollama_host,
            model=model or settings.ollama_model,
            num_ctx=num_ctx or settings.ollama_num_ctx,
            timeout=settings.ollama_timeout,
            num_threads=num_threads or settings.ollama_threads,
            keep_alive=settings.ollama_keep_alive,
        )
    else:
        raise SystemExit(f"Unknown LLM_PROVIDER '{name}'. Use openai or ollama.")
    llm.check()
    return llm


def default_workers(llm) -> int:
    """Cloud calls are network-bound; local CPU calls contend for cores.

    Cap at LLM_MAX_CONCURRENCY so "10 workers" cannot exceed the shared gate.
    """
    if isinstance(llm, OpenAICompat):
        return llm_max_concurrency()
    return 1
