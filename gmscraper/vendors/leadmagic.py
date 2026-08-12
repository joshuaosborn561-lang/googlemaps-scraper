"""LeadMagic client — email finder + people search (DM discovery)."""

from __future__ import annotations

import os
from typing import Any, Sequence

import requests

from .base import EmailHit, PersonHit, split_name

# LeadMagic people-search accepts at most 12 title terms.
_MAX_TITLES = 12


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _norm_titles(titles: Sequence[str] | None) -> list[str]:
    """Cap + dedupe titles for /v3/people/search (max 12)."""
    if not titles:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in titles:
        t = str(raw or "").strip()
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        # API examples use Title Case; keep acronyms like GM / VP uppercase.
        if key in {"gm", "vp", "ceo", "coo", "cfo", "cto", "cmo"}:
            out.append(key.upper())
        elif t.islower():
            out.append(t.title())
        else:
            out.append(t)
        if len(out) >= _MAX_TITLES:
            break
    return out


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
        self,
        domain: str,
        company_name: str = "",
        limit: int = 10,
        titles: Sequence[str] | None = None,
        *,
        include_contact_details: bool = False,
    ) -> list[PersonHit]:
        """DM discovery via POST /v3/people/search (not role-finder).

        Pass ``titles`` (up to 12) — e.g. Service Director, Service Manager.
        Without titles the call is skipped (broad search burns credits poorly).
        Contact-detail unlocks stay off by default (1 credit / person; email
        finder fills emails in a later step).
        """
        if not self.enabled or not domain:
            return []
        title_list = _norm_titles(titles)
        if not title_list:
            return []

        body: dict[str, Any] = {
            "company_domain": domain,
            "titles": title_list,
            "limit": max(1, min(int(limit or 10), 50)),
            "include_contact_details": bool(include_contact_details),
        }
        if company_name:
            body["company_name"] = company_name

        self.calls += 1
        try:
            r = requests.post(
                f"{self.base_url}/v3/people/search",
                json=body,
                headers=self._headers(),
                timeout=self.timeout,
            )
            if r.status_code >= 400:
                return []
            data = r.json()
        except (requests.RequestException, ValueError):
            return []

        rows = []
        if isinstance(data, dict):
            rows = data.get("people") or data.get("data") or data.get("results") or []
        elif isinstance(data, list):
            rows = data

        out: list[PersonHit] = []
        for row in (rows or [])[:limit]:
            if not isinstance(row, dict):
                continue
            first = str(
                row.get("contact_first_name")
                or row.get("first_name")
                or ""
            ).strip()
            last = str(
                row.get("contact_last_name")
                or row.get("last_name")
                or ""
            ).strip()
            full = str(
                row.get("contact_full_name")
                or row.get("full_name")
                or row.get("name")
                or ""
            ).strip()
            if not first and full:
                first, last = split_name(full)
            if not (first or full):
                continue
            title = str(
                row.get("contact_job_title")
                or row.get("title")
                or row.get("job_title")
                or ""
            )
            email = str(
                row.get("contact_email")
                or row.get("email")
                or ""
            ).strip().lower()
            linkedin = str(
                row.get("contact_linkedin_url")
                or row.get("linkedin_url")
                or row.get("profile_url")
                or ""
            )
            out.append(
                PersonHit(
                    first_name=first,
                    last_name=last,
                    full_name=full or f"{first} {last}".strip(),
                    title=title,
                    email=email,
                    linkedin_url=linkedin,
                    job_level=str(row.get("contact_job_level") or ""),
                    source_tier=self.tier,
                    raw=row,
                )
            )
        if out:
            self.hits += 1
        return out
