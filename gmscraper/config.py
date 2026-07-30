"""Environment-backed settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "leads.db"
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


@dataclass
class Settings:
    rapidapi_key: str = field(default_factory=lambda: _env("RAPIDAPI_KEY"))
    maps_host: str = field(
        default_factory=lambda: _env("MAPS_DATA_HOST", "maps-data.p.rapidapi.com")
    )
    maps_path: str = field(
        default_factory=lambda: _env("MAPS_DATA_PATH", "/searchmaps.php")
    )

    ollama_host: str = field(
        default_factory=lambda: _env("OLLAMA_HOST", "http://localhost:11434")
    )
    ollama_model: str = field(
        default_factory=lambda: _env("OLLAMA_MODEL", "gemma4:12b")
    )

    owj_key: str = field(default_factory=lambda: _env("OPENWEBNINJA_KEY"))
    owj_host: str = field(
        default_factory=lambda: _env(
            "OPENWEBNINJA_HOST", "real-time-web-search.p.rapidapi.com"
        )
    )
    owj_path: str = field(default_factory=lambda: _env("OPENWEBNINJA_PATH", "/search"))

    maps_plan: str = field(default_factory=lambda: _env("MAPS_PLAN", "pro").lower())

    @property
    def plan(self) -> Plan:
        return PLANS.get(self.maps_plan, PLANS["pro"])

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
