"""GetLeads client — waterfall tier 1 (email + people discovery).

GetLeads resolves work emails and profiles that AI Ark reverse-lookup misses.
Base URL and paths are env-configurable because the public API surface varies
by account; defaults target app.getleads.io.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from .base import EmailHit, PersonHit, split_name


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


class GetLeadsClient:
    tier = "getleads"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 45,
    ):
        self.api_key = api_key if api_key is not None else _env("GETLEADS_API_KEY")
        self.base_url = (
            base_url
            if base_url is not None
            else _env("GETLEADS_BASE_URL", "https://app.getleads.io/api")
        ).rstrip("/")
        self.find_email_path = _env("GETLEADS_FIND_EMAIL_PATH", "/find-email")
        self.enrich_email_path = _env("GETLEADS_ENRICH_EMAIL_PATH", "/enrich-email")
        self.people_path = _env("GETLEADS_PEOPLE_PATH", "/people")
        self.timeout = timeout
        self.calls = 0
        self.hits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        self.calls += 1
        try:
            r = requests.post(
                url, json=body, headers=self._headers(), timeout=self.timeout
            )
            if r.status_code >= 400:
                return None
            data = r.json()
            return data if isinstance(data, dict) else {"data": data}
        except (requests.RequestException, ValueError):
            return None

    def find_email(
        self, first_name: str, last_name: str, domain: str, company_name: str = ""
    ) -> EmailHit | None:
        body = {
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
            "company_name": company_name or domain,
            "company": company_name or domain,
        }
        data = self._post(self.find_email_path, body)
        if not data:
            return None
        email = (
            data.get("email")
            or (data.get("data") or {}).get("email")
            or (data.get("result") or {}).get("email")
            or ""
        )
        email = str(email).strip().lower()
        if not email or "@" not in email:
            return None
        self.hits += 1
        return EmailHit(
            email=email,
            source_tier=self.tier,
            status=str(data.get("status") or "found"),
            raw=data,
        )

    def enrich_by_email(self, email: str) -> PersonHit | None:
        """Profile from work email (getleads resolves records AI Ark 404s)."""
        data = self._post(self.enrich_email_path, {"email": email})
        if not data:
            return None
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        first = str(payload.get("first_name") or "").strip()
        last = str(payload.get("last_name") or "").strip()
        full = str(payload.get("full_name") or payload.get("name") or "").strip()
        if not first and full:
            first, last = split_name(full)
        if not (first or full):
            return None
        self.hits += 1
        return PersonHit(
            first_name=first,
            last_name=last,
            full_name=full or f"{first} {last}".strip(),
            title=str(payload.get("title") or payload.get("job_title") or ""),
            job_level=str(payload.get("job_level") or payload.get("seniority") or ""),
            email=email,
            linkedin_url=str(payload.get("linkedin_url") or payload.get("linkedin") or ""),
            source_tier=self.tier,
            raw=payload if isinstance(payload, dict) else data,
        )

    def find_people(
        self, domain: str, company_name: str = "", limit: int = 5
    ) -> list[PersonHit]:
        data = self._post(
            self.people_path,
            {
                "domain": domain,
                "company_name": company_name or domain,
                "limit": limit,
            },
        )
        if not data:
            return []
        rows = data.get("people") or data.get("data") or data.get("results") or []
        if isinstance(rows, dict):
            rows = rows.get("people") or rows.get("results") or []
        out: list[PersonHit] = []
        for row in rows[:limit]:
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
                    job_level=str(row.get("job_level") or row.get("seniority") or ""),
                    email=str(row.get("email") or "").strip().lower(),
                    linkedin_url=str(row.get("linkedin_url") or row.get("linkedin") or ""),
                    source_tier=self.tier,
                    raw=row,
                )
            )
        if out:
            self.hits += 1
        return out
