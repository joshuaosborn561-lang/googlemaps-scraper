"""Environment-backed settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "leads.db"
DEFAULT_ZIPS = ROOT / "data" / "us_zipcodes.csv"
DEFAULT_CATEGORIES = ROOT / "config" / "categories.yml"


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

    price_per_request: float = field(
        default_factory=lambda: _env_float("PRICE_PER_REQUEST", 100.0 / 3_000_000)
    )

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
