"""Local Ollama client.

Uses Ollama's structured-output support: `format` takes a JSON schema and the
model is constrained to emit matching JSON, so there is no brittle parsing of
prose.  `keep_alive` holds the model in VRAM between calls -- without it a
long run pays the reload cost on every request.
"""

from __future__ import annotations

import json
from typing import Any

import requests


class OllamaError(RuntimeError):
    pass


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


class Ollama:
    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = "gemma4:e4b",
        timeout: int = 600,
        keep_alive: str = "10m",
        num_ctx: int = 4096,
        num_threads: int = 0,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self.num_threads = num_threads
        self.session = requests.Session()
        # Populated from the last call: Ollama reports prefill and generation
        # separately, which is the only way to tell a too-big-prompt problem
        # from a too-big-model problem.
        self.last: dict[str, float] = {}

    # ------------------------------------------------------------ preflight

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

        # Ollama reports "gemma4:12b"; accept a bare "gemma4" as a match too.
        if not any(t == self.model or t.split(":")[0] == self.model.split(":")[0]
                   for t in tags):
            raise SystemExit(
                f"Model '{self.model}' is not pulled. Run:\n"
                f"    ollama pull {self.model}\n"
                f"Installed: {', '.join(tags) or '(none)'}"
            )

    # ----------------------------------------------------------------- chat

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
        except requests.Timeout as exc:
            raise OllamaError(
                f"ollama timed out after {self.timeout}s. On CPU-only hardware "
                f"try a smaller model (gemma4:e4b, qwen3.5:4b, phi4-mini) and "
                f"a shorter prompt -- run `python -m gmscraper bench`."
            ) from exc
        except requests.RequestException as exc:
            raise OllamaError(f"ollama request failed: {exc}") from exc

        if not content:
            raise OllamaError("empty response from model")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise OllamaError(f"model returned non-JSON: {content[:200]}") from exc
