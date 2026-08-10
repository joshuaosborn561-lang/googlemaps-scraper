"""AI Ark client — waterfall tier 2 for people discovery ONLY.

Do NOT use for email-to-profile reverse lookup (404s on records getleads
resolves cleanly). People Search by company domain → name/title/linkedin.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from .base import PersonHit, split_name


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


class AiArkClient:
    tier = "ai_ark"
    base_url = "https://api.ai-ark.com/api/developer-portal"

    def __init__(self, api_key: str | None = None, timeout: int = 45):
        self.api_key = api_key if api_key is not None else (
            _env("AI_ARK_API_KEY") or _env("AIARK_API_KEY")
        )
        self.timeout = timeout
        self.calls = 0
        self.hits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-TOKEN": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def find_people(
        self,
        domain: str,
        *,
        company_name: str = "",
        limit: int = 5,
        seniorities: list[str] | None = None,
    ) -> list[PersonHit]:
        """People Search filtered to a company domain — no email export."""
        if not self.enabled or not domain:
            return []
        body: dict[str, Any] = {
            "page": 0,
            "size": max(1, min(int(limit), 25)),
            "account": {
                "domain": {
                    "any": {"include": [domain]},
                }
            },
        }
        if seniorities:
            body["contact"] = {
                "seniority": {"any": {"include": seniorities}},
            }
        self.calls += 1
        try:
            r = requests.post(
                f"{self.base_url}/v1/people",
                json=body,
                headers=self._headers(),
                timeout=self.timeout,
            )
            if r.status_code >= 400:
                return []
            data = r.json()
        except (requests.RequestException, ValueError):
            return []

        content = []
        if isinstance(data, dict):
            content = data.get("content") or data.get("data") or data.get("results") or []
            if isinstance(content, dict):
                content = content.get("content") or content.get("results") or []
        out: list[PersonHit] = []
        for row in content[:limit]:
            if not isinstance(row, dict):
                continue
            profile = row.get("profile") if isinstance(row.get("profile"), dict) else row
            first = str(profile.get("first_name") or "").strip()
            last = str(profile.get("last_name") or "").strip()
            full = str(profile.get("full_name") or "").strip()
            # AI Ark sometimes packs title into last_name — strip obvious junk.
            if "," in last:
                last = last.split(",", 1)[0].strip()
            if not first and full:
                first, last = split_name(full)
            if not (first or full):
                continue
            title = str(
                profile.get("title")
                or profile.get("headline")
                or row.get("title")
                or ""
            )
            link = row.get("link") if isinstance(row.get("link"), dict) else {}
            linkedin = str(
                link.get("linkedin")
                or row.get("linkedin_url")
                or profile.get("linkedin_url")
                or ""
            )
            out.append(
                PersonHit(
                    first_name=first,
                    last_name=last,
                    full_name=full or f"{first} {last}".strip(),
                    title=title,
                    job_level=str(profile.get("seniority") or row.get("seniority") or ""),
                    linkedin_url=linkedin,
                    source_tier=self.tier,
                    raw=row,
                )
            )
        if out:
            self.hits += 1
        return out
