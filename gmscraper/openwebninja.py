"""OpenWeb Ninja web-search fallback (optional, paid, cheap).

Only used when the website scrape finds no owner name. Like the Maps client,
this does not hard-code a response shape -- it walks the JSON and collects the
text-bearing fields (titles, snippets, and any AI-overview block), which is
all the local model needs to read.
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


def _harvest(node: Any, out: list[str], depth: int = 0) -> None:
    if depth > 6 or len(out) > 60:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and k.replace("_", "").lower() in TEXT_KEYS:
                s = v.strip()
                if len(s) > 2:
                    out.append(s)
            else:
                _harvest(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node[:20]:
            _harvest(v, out, depth + 1)


class OpenWebNinja:
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
        """Return the search results flattened to plain text ('' if disabled)."""
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
                _harvest(r.json(), chunks)
                return "\n".join(chunks)[:MAX_CHARS]
            except (requests.RequestException, ValueError):
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 20))
        return ""
