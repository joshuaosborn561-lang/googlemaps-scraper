"""Environment-backed settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ZIPS = ROOT / "data" / "us_zipcodes.csv"
DEFAULT_CATEGORIES = ROOT / "config" / "categories.yml"


@dataclass(frozen=True)
class Plan:
    """A Maps Data subscription tier.

    Billing is a monthly fee that includes a request quota, then a per-request
    overage. A flat cents-per-request number cannot express that: the same
    10,000 searches cost $0 with quota left and $10 without, which is exactly
    the difference that decides whether a run should auto-approve.
    """

    name: str
    monthly_usd: float
    included: int
    overage_usd: float | None      # None = hard limit, requests just fail

    def cost_for(self, requests: int, already_used: int = 0) -> tuple[float, int]:
        """(overage cost, billable requests) for `requests` more this month."""
        remaining = max(0, self.included - already_used)
        billable = max(0, requests - remaining)
        if billable == 0:
            return 0.0, 0
        if self.overage_usd is None:
            return float("inf"), billable
        return billable * self.overage_usd, billable


# Maps Data on RapidAPI, as listed on the plan page.
PLANS: dict[str, Plan] = {
    "basic": Plan("basic", 0.0, 1_000, None),
    "pro": Plan("pro", 5.0, 30_000, 0.001),
    "ultra": Plan("ultra", 25.0, 300_000, 0.0009),
    "mega": Plan("mega", 250.0, 6_000_000, 0.00005),
}


def cycle_start(reset_day: int = 1, today: "date | None" = None) -> str:
    """First day of the current billing cycle, as an ISO date.

    RapidAPI resets quota on the subscription anniversary, not the 1st. If you
    subscribed on the 30th, then on the 5th of the next month you are five days
    into a cycle -- counting from the 1st would under-report usage and make an
    over-quota run look free. Days past 28 are clamped so February behaves.
    """
    from datetime import date as _date, timedelta

    today = today or _date.today()
    day = max(1, min(int(reset_day), 28))
    if today.day >= day:
        return today.replace(day=day).isoformat()
    prev_month_end = today.replace(day=1) - timedelta(days=1)
    return prev_month_end.replace(day=day).isoformat()


def cheapest_plan(requests: int, used_this_month: int = 0) -> tuple[Plan, float]:
    """(plan, month total) for the cheapest tier that can serve this run.

    Overage means a small plan never blocks a big run, it just quietly bills
    for it -- a national sweep on `pro` costs $331 instead of $75 on `ultra`.
    Worth surfacing rather than leaving to be discovered on the invoice.
    """
    best: tuple[Plan, float] | None = None
    for plan in PLANS.values():
        overage, _ = plan.cost_for(requests, used_this_month)
        if overage == float("inf"):
            continue                       # hard-limit plan cannot serve it
        total = plan.monthly_usd + overage
        if best is None or total < best[1]:
            best = (plan, total)
    return best or (PLANS["mega"], PLANS["mega"].monthly_usd)


def _load_dotenv() -> None:
    """Load .env if python-dotenv is installed; fall back to a tiny parser."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
        return
    except ImportError:
        pass
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


# Prefer data/ so a Railway volume at /app/data keeps the DB across deploys.
# Fall back to repo-root leads.db when that legacy path already exists.
def _default_db() -> Path:
    override = _env("LEADS_DB")
    if override:
        return Path(override)
    data_db = ROOT / "data" / "leads.db"
    legacy = ROOT / "leads.db"
    if data_db.exists() or not legacy.exists():
        return data_db
    return legacy


DEFAULT_DB = _default_db()


@dataclass
class Settings:
    rapidapi_key: str = field(default_factory=lambda: _env("RAPIDAPI_KEY"))
    maps_host: str = field(
        default_factory=lambda: _env("MAPS_DATA_HOST", "maps-data.p.rapidapi.com")
    )
    maps_path: str = field(
        default_factory=lambda: _env("MAPS_DATA_PATH", "/searchmaps.php")
    )

    # openai (any OpenAI-shaped endpoint) | ollama (local)
    llm_provider: str = field(
        default_factory=lambda: _env("LLM_PROVIDER", "openai").lower()
    )
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_model: str = field(
        default_factory=lambda: _env("OPENAI_MODEL", "gpt-5-nano")
    )
    openai_base_url: str = field(
        default_factory=lambda: _env("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    # USD per million tokens, for the spend line the stages print. Update
    # these if you change model or provider -- they are reporting only.
    openai_price_in: float = field(
        default_factory=lambda: _env_float("OPENAI_PRICE_IN", 0.05)
    )
    openai_price_out: float = field(
        default_factory=lambda: _env_float("OPENAI_PRICE_OUT", 0.40)
    )

    ollama_host: str = field(
        default_factory=lambda: _env("OLLAMA_HOST", "http://localhost:11434")
    )
    # gemma4:e4b is the edge-sized Gemma: ~4.7 GB, runs on CPU. The 12b needs
    # a GPU to be practical -- see `bench`.
    ollama_model: str = field(
        default_factory=lambda: _env("OLLAMA_MODEL", "gemma4:e4b")
    )
    ollama_num_ctx: int = field(
        default_factory=lambda: int(_env_float("OLLAMA_NUM_CTX", 4096))
    )
    ollama_timeout: int = field(
        default_factory=lambda: int(_env_float("OLLAMA_TIMEOUT", 600))
    )
    # 0 = let Ollama decide (it grabs most cores). Cap it to keep the machine
    # usable while a stage runs -- inference is a background job, not the
    # thing you are looking at.
    ollama_threads: int = field(
        default_factory=lambda: int(_env_float("OLLAMA_NUM_THREADS", 0))
    )
    # How long Ollama pins the weights in RAM after the last call. Long is
    # right mid-run (no reload per business); short frees several GB the
    # moment a stage finishes.
    ollama_keep_alive: str = field(
        default_factory=lambda: _env("OLLAMA_KEEP_ALIVE", "10m")
    )
    # Characters of page text sent to the model. Prefill dominates on CPU, so
    # this is the single biggest lever on runtime.
    max_evidence_chars: int = field(
        default_factory=lambda: int(_env_float("LLM_MAX_EVIDENCE_CHARS", 2500))
    )

    owj_key: str = field(default_factory=lambda: _env("OPENWEBNINJA_KEY"))
    owj_host: str = field(
        default_factory=lambda: _env(
            "OPENWEBNINJA_HOST", "real-time-web-search.p.rapidapi.com"
        )
    )
    owj_path: str = field(default_factory=lambda: _env("OPENWEBNINJA_PATH", "/search"))

    # Owner-name fallback: apify (ScraperLink SERP) | openwebninja | none
    fallback_source: str = field(
        default_factory=lambda: _env("FALLBACK_SOURCE", "apify").lower()
    )
    apify_token: str = field(default_factory=lambda: _env("APIFY_TOKEN"))
    apify_serp_actor: str = field(
        default_factory=lambda: _env(
            "APIFY_SERP_ACTOR", "scraperlink/google-search-results-serp-scraper"
        )
    )
    apify_contact_actor: str = field(
        default_factory=lambda: _env(
            "APIFY_CONTACT_ACTOR", "automation-lab/website-contact-finder"
        )
    )
    apify_content_actor: str = field(
        default_factory=lambda: _env(
            "APIFY_CONTENT_ACTOR", "apify/website-content-crawler"
        )
    )
    apify_max_cost_usd: float = field(
        default_factory=lambda: _env_float("APIFY_MAX_COST_USD", 5.0)
    )
    apify_base_url: str = field(
        default_factory=lambda: _env("APIFY_BASE_URL", "https://api.apify.com").rstrip("/")
    )

    maps_plan: str = field(default_factory=lambda: _env("MAPS_PLAN", "ultra").lower())
    quota_reset_day: int = field(
        default_factory=lambda: int(_env_float("MAPS_QUOTA_RESET_DAY", 1))
    )

    # Contact enrichment waterfall (getleads → AI Ark → LeadMagic → FullEnrich)
    getleads_api_key: str = field(default_factory=lambda: _env("GETLEADS_API_KEY"))
    ai_ark_api_key: str = field(
        default_factory=lambda: _env("AI_ARK_API_KEY") or _env("AIARK_API_KEY")
    )
    leadmagic_api_key: str = field(
        default_factory=lambda: _env("LEADMAGIC_API_KEY") or _env("LEADMAGIC_KEY")
    )
    fullenrich_api_key: str = field(default_factory=lambda: _env("FULLENRICH_API_KEY"))

    @property
    def plan(self) -> Plan:
        return PLANS.get(self.maps_plan, PLANS["ultra"])

    @property
    def price_per_request(self) -> float:
        """Marginal cost once the monthly quota is gone — the honest number
        to price a large run against."""
        override = _env_float("PRICE_PER_REQUEST", 0.0)
        if override:
            return override
        return self.plan.overage_usd or 0.0

    @staticmethod
    def _url(host: str, path: str) -> str:
        # A host may carry its own scheme (handy for pointing at a local mock);
        # otherwise RapidAPI is always https.
        return f"{host}{path}" if "://" in host else f"https://{host}{path}"

    @property
    def maps_url(self) -> str:
        return self._url(self.maps_host, self.maps_path)

    @property
    def owj_url(self) -> str:
        return self._url(self.owj_host, self.owj_path)

    def require_rapidapi(self) -> None:
        if not self.rapidapi_key:
            raise SystemExit(
                "RAPIDAPI_KEY is not set. Copy .env.example to .env and add your key "
                "from https://rapidapi.com/alexanderxbx/api/maps-data"
            )


settings = Settings()
