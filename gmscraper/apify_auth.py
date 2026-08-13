"""Shared Apify token validation (SERP / owner fallback — not contact crawl)."""

from __future__ import annotations

import requests

from .config import settings


def apify_token_valid(token: str | None = None, timeout: int = 15) -> bool:
    tok = (token if token is not None else settings.apify_token) or ""
    if not tok.strip():
        return False
    try:
        r = requests.get(
            f"{settings.apify_base_url}/v2/users/me",
            params={"token": tok},
            timeout=timeout,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False
