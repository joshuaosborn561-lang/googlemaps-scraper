"""LeadMagic client — waterfall tier 3 (email finder)."""

from __future__ import annotations

import os
from typing import Any

import requests

from .base import EmailHit, PersonHit, split_name


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


class LeadMagicClient:
    tier = "leadmagic"
    base_url = "https://api.leadmagic.io"

    def __init__(self, api_key: str | None = None, timeout: int = 45):
        self.api_key = api_key if api_key is not None else (
            _env("LEADMAGIC_API_KEY") or _env("LEADMAGIC_KEY")
        )
        self.timeout = timeout
        self.calls = 0
        self.hits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def find_email(
        self, first_name: str, last_name: str, domain: str, company_name: str = ""
    ) -> EmailHit | None:
        if not self.enabled:
            return None
        body: dict[str, Any] = {
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
        }
        if company_name:
            body["company_name"] = company_name
        self.calls += 1
        try:
            r = requests.post(
                f"{self.base_url}/v1/people/email-finder",
                json=body,
                headers=self._headers(),
                timeout=self.timeout,
            )
            if r.status_code >= 400:
                return None
            data = r.json()
        except (requests.RequestException, ValueError):
            return None
        email = str(data.get("email") or "").strip().lower()
        status = str(data.get("status") or "")
        if not email or status in {"not_found", "invalid"}:
            return None
        self.hits += 1
        return EmailHit(
            email=email,
            source_tier=self.tier,
            status=status or "valid",
            raw=data if isinstance(data, dict) else {},
        )

    def find_people(
        self, domain: str, company_name: str = "", limit: int = 5
    ) -> list[PersonHit]:
        """Optional role search — best-effort; email-finder is the primary use."""
        if not self.enabled or not domain:
            return []
        body = {
            "company_domain": domain,
            "domain": domain,
            "company_name": company_name or domain,
            "limit": limit,
        }
        self.calls += 1
        try:
            r = requests.post(
                f"{self.base_url}/v1/people/role-finder",
                json=body,
                headers=self._headers(),
                timeout=self.timeout,
            )
            if r.status_code >= 400:
                return []
            data = r.json()
        except (requests.RequestException, ValueError):
            return []
        rows = data.get("data") or data.get("people") or data.get("results") or []
        if isinstance(data, list):
            rows = data
        out: list[PersonHit] = []
        for row in (rows or [])[:limit]:
            if not isinstance(row, dict):
                continue
            first = str(row.get("first_name") or "").strip()
            last = str(row.get("last_name") or "").strip()
            full = str(row.get("full_name") or row.get("name") or "").strip()
            if not first and full:
                first, last = split_name(full)
            if not (first or full):
                continue
            out.append(
                PersonHit(
                    first_name=first,
                    last_name=last,
                    full_name=full or f"{first} {last}".strip(),
                    title=str(row.get("title") or row.get("job_title") or ""),
                    email=str(row.get("email") or "").strip().lower(),
                    linkedin_url=str(row.get("linkedin_url") or row.get("profile_url") or ""),
                    source_tier=self.tier,
                    raw=row,
                )
            )
        if out:
            self.hits += 1
        return out
