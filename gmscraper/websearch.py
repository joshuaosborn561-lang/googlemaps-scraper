"""Web-search backends for the owner-name fallback.

When the website scrape finds no owner, we Google "who owns <business> in
<city>" and let the local model read the results. Backends implement the
same tiny interface (`enabled`, `request_count`, `cost_per_search`,
`search_text(query, limit)`), so `owners` does not care which one is
configured.

Default is Serper (`POST https://google.serper.dev/search`) at $0.001 per
search. Apify ScraperLink and OpenWeb Ninja stay available via
`FALLBACK_SOURCE`. If `FALLBACK_SOURCE=serper` and `SERPER_API_KEY` is
blank, the factory returns `NullSearch` — it does not silently fall back
to Apify.

None of the backends return Google's AI Overview as a paid add-on. Serper
organic titles/snippets (plus answerBox and knowledgeGraph descriptions
when present) are harvested into the same text shape the extractor already
reads.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from .config import Settings

# Fields worth showing the model; everything else in the payload is plumbing.
TEXT_KEYS = {
    "title", "snippet", "description", "text", "answer", "content",
    "ai_overview", "aioverview", "overview", "summary", "body",
}
MAX_CHARS = 4000
SERPER_URL = "https://google.serper.dev/search"


def harvest_text(node: Any, out: list[str], depth: int = 0) -> None:
    """Walk a search response and collect the human-readable strings."""
    if depth > 6 or len(out) > 60:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and k.replace("_", "").lower() in TEXT_KEYS:
                s = v.strip()
                if len(s) > 2:
                    out.append(s)
            else:
                harvest_text(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node[:20]:
            harvest_text(v, out, depth + 1)


class NullSearch:
    """No backend configured -- `owners` runs website-only."""

    enabled = False
    request_count = 0
    cost_per_search = 0.0

    def search_text(self, query: str, limit: int = 10) -> str:
        return ""


class SerperSearch:
    """Serper.dev Google Search. Default owner-name fallback."""

    cost_per_search = 0.001

    def __init__(
        self,
        settings: Settings,
        timeout: int = 25,
        max_retries: int = 3,
    ):
        self.s = settings
        self.timeout = timeout
        self.max_retries = max_retries
        self.enabled = bool(settings.serper_api_key)
        self.request_count = 0
        self.session = requests.Session()
        if self.enabled:
            self.session.headers.update(
                {
                    "X-API-KEY": settings.serper_api_key,
                    "Content-Type": "application/json",
                }
            )

    def search_text(self, query: str, limit: int = 10) -> str:
        if not self.enabled:
            return ""
        payload = {"q": query, "gl": "us", "hl": "en", "num": limit}
        for attempt in range(self.max_retries + 1):
            try:
                self.request_count += 1
                r = self.session.post(
                    SERPER_URL,
                    json=payload,
                    timeout=self.timeout,
                )
                if r.status_code in (401, 403):
                    return ""             # bad key; fail quiet, not loud
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                r.raise_for_status()
                chunks: list[str] = []
                harvest_text(r.json(), chunks)
                return "\n".join(chunks)[:MAX_CHARS]
            except (requests.RequestException, ValueError):
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 20))
        return ""


class ApifySerp:
    """ScraperLink's Google SERP actor, run one query at a time.

    `limit` is pinned to a single page. The actor's own default is "all",
    which walks every page of results and bills per page -- an easy way to
    turn a $0.0005 lookup into a much larger one for snippets we would throw
    away anyway.
    """

    cost_per_search = 0.0005

    def __init__(
        self,
        settings: Settings,
        timeout: int = 120,
        max_retries: int = 2,
    ):
        self.s = settings
        self.timeout = timeout
        self.max_retries = max_retries
        self.enabled = bool(settings.apify_token)
        self.request_count = 0
        self.session = requests.Session()

    @property
    def url(self) -> str:
        actor = self.s.apify_serp_actor.replace("/", "~")
        return (
            f"{self.s.apify_base_url}/v2/acts/{actor}/run-sync-get-dataset-items"
        )

    def search_text(self, query: str, limit: int = 10) -> str:
        if not self.enabled:
            return ""
        payload = {
            "keyword": query,
            "limit": str(limit),          # never "all" -- see class docstring
            "include_merged": False,
            "country": "US",
            "hl": "en",
        }
        for attempt in range(self.max_retries + 1):
            try:
                self.request_count += 1
                r = self.session.post(
                    self.url,
                    params={"token": self.s.apify_token},
                    json=payload,
                    timeout=self.timeout,
                )
                if r.status_code in (401, 403):
                    return ""             # bad token; fail quiet, not loud
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                r.raise_for_status()
                chunks: list[str] = []
                harvest_text(r.json(), chunks)
                return "\n".join(chunks)[:MAX_CHARS]
            except (requests.RequestException, ValueError):
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 20))
        return ""


class OpenWebNinja:
    """RapidAPI Real-Time Web Search. Kept for anyone already subscribed."""

    cost_per_search = 0.0025

    def __init__(self, settings: Settings, timeout: int = 25, max_retries: int = 3):
        self.s = settings
        self.timeout = timeout
        self.max_retries = max_retries
        self.enabled = bool(settings.owj_key)
        self.request_count = 0
        self.session = requests.Session()
        if self.enabled:
            self.session.headers.update(
                {
                    "x-rapidapi-key": settings.owj_key,
                    "x-rapidapi-host": settings.owj_host,
                    "Accept": "application/json",
                }
            )

    def search_text(self, query: str, limit: int = 10) -> str:
        if not self.enabled:
            return ""
        params = {"q": query, "limit": str(limit), "gl": "us", "hl": "en"}
        for attempt in range(self.max_retries + 1):
            try:
                self.request_count += 1
                r = self.session.get(self.s.owj_url, params=params, timeout=self.timeout)
                if r.status_code in (401, 403):
                    return ""
                if r.status_code == 429:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                r.raise_for_status()
                chunks: list[str] = []
                harvest_text(r.json(), chunks)
                return "\n".join(chunks)[:MAX_CHARS]
            except (requests.RequestException, ValueError):
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 20))
        return ""


BACKENDS = {
    "serper": SerperSearch,
    "apify": ApifySerp,
    "openwebninja": OpenWebNinja,
    "none": NullSearch,
}


def make_backend(settings: Settings, source: str = ""):
    """Build the configured backend. Unknown/absent credentials -> NullSearch."""
    name = (source or settings.fallback_source or "serper").lower()
    cls = BACKENDS.get(name)
    if cls is None:
        raise SystemExit(
            f"Unknown fallback source '{name}'. Options: {', '.join(BACKENDS)}"
        )
    if cls is NullSearch:
        return NullSearch()
    backend = cls(settings)
    if not backend.enabled:
        return NullSearch()
    return backend
