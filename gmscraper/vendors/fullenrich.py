"""FullEnrich client — waterfall tier 4 (email only).

Bills 1 credit per work email found, 0 on miss.
API: POST /contact/enrich/bulk then poll GET /contact/enrich/bulk/{id}
Env: FULLENRICH_API_KEY
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from .base import EmailHit


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


class FullEnrichClient:
    tier = "fullenrich"
    base_url = "https://app.fullenrich.com/api/v2"

    def __init__(self, api_key: str | None = None, timeout: int = 60):
        self.api_key = api_key if api_key is not None else _env("FULLENRICH_API_KEY")
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.credits_used = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def find_email(
        self,
        first_name: str,
        last_name: str,
        domain: str,
        company_name: str = "",
        *,
        poll_seconds: float = 2.0,
        max_wait: float = 90.0,
    ) -> EmailHit | None:
        results = self.find_email_bulk(
            [
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "domain": domain,
                    "company_name": company_name,
                }
            ],
            poll_seconds=poll_seconds,
            max_wait=max_wait,
        )
        return results[0] if results else None

    def find_email_bulk(
        self,
        rows: list[dict[str, str]],
        *,
        name: str = "gmscraper email enrich",
        poll_seconds: float = 3.0,
        max_wait: float = 180.0,
    ) -> list[EmailHit | None]:
        """Enrich up to 100 contacts. Returns parallel list (None = miss)."""
        if not self.enabled or not rows:
            return [None] * len(rows)

        data = []
        for i, row in enumerate(rows[:100]):
            first = (row.get("first_name") or "").strip()
            last = (row.get("last_name") or "").strip()
            domain = (row.get("domain") or "").strip()
            if not first or not last or not domain:
                continue
            data.append(
                {
                    "first_name": first,
                    "last_name": last,
                    "domain": domain,
                    "company_name": (row.get("company_name") or domain).strip(),
                    "enrich_fields": ["contact.work_emails"],
                    "custom": {"idx": str(i)},
                }
            )
        if not data:
            return [None] * len(rows)

        self.calls += 1
        try:
            r = requests.post(
                f"{self.base_url}/contact/enrich/bulk",
                json={"name": name, "data": data},
                headers=self._headers(),
                timeout=self.timeout,
            )
            if r.status_code >= 400:
                return [None] * len(rows)
            accepted = r.json()
        except (requests.RequestException, ValueError):
            return [None] * len(rows)

        enrichment_id = (
            accepted.get("enrichment_id")
            or accepted.get("id")
            or (accepted.get("data") or {}).get("enrichment_id")
        )
        if not enrichment_id:
            return [None] * len(rows)

        finished = self._poll(str(enrichment_id), poll_seconds=poll_seconds, max_wait=max_wait)
        by_idx: dict[int, EmailHit] = {}
        for contact in finished:
            email = _work_email_from_contact(contact)
            if not email:
                continue
            custom = contact.get("custom") or {}
            try:
                idx = int(custom.get("idx", -1))
            except (TypeError, ValueError):
                idx = -1
            hit = EmailHit(
                email=email,
                source_tier=self.tier,
                status=_email_status(contact),
                raw=contact if isinstance(contact, dict) else {},
            )
            self.hits += 1
            self.credits_used += 1
            if idx >= 0:
                by_idx[idx] = hit

        return [by_idx.get(i) for i in range(len(rows))]

    def _poll(
        self, enrichment_id: str, *, poll_seconds: float, max_wait: float
    ) -> list[dict[str, Any]]:
        deadline = time.time() + max_wait
        url = f"{self.base_url}/contact/enrich/bulk/{enrichment_id}"
        while time.time() < deadline:
            try:
                r = requests.get(url, headers=self._headers(), timeout=self.timeout)
                if r.status_code >= 400:
                    time.sleep(poll_seconds)
                    continue
                payload = r.json()
            except (requests.RequestException, ValueError):
                time.sleep(poll_seconds)
                continue
            status = str(
                payload.get("status")
                or payload.get("enrichment_status")
                or ""
            ).upper()
            contacts = (
                payload.get("datas")
                or payload.get("data")
                or payload.get("contacts")
                or payload.get("results")
                or []
            )
            if isinstance(contacts, dict):
                contacts = contacts.get("contacts") or contacts.get("results") or []
            # FullEnrich bulk GET returns status FINISHED|IN_PROGRESS and data=[...]
            if status in {"FINISHED", "DONE", "COMPLETED", "SUCCESS"}:
                return [c for c in contacts if isinstance(c, dict)]
            if status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
                return []
            time.sleep(poll_seconds)
        return []


def _work_email_from_contact(contact: dict[str, Any]) -> str:
    info = contact.get("contact_info") or contact.get("contact") or contact
    emails = []
    if isinstance(info, dict):
        emails = info.get("work_emails") or info.get("emails") or []
    if not emails and contact.get("email"):
        return str(contact.get("email")).strip().lower()
    for item in emails:
        if isinstance(item, str) and "@" in item:
            return item.strip().lower()
        if isinstance(item, dict):
            e = str(item.get("email") or "").strip().lower()
            if e:
                return e
    most = contact.get("most_probable_email") or contact.get("work_email")
    if most:
        return str(most).strip().lower()
    return ""


def _email_status(contact: dict[str, Any]) -> str:
    info = contact.get("contact_info") or {}
    emails = info.get("work_emails") or []
    if emails and isinstance(emails[0], dict):
        return str(emails[0].get("status") or "found")
    return "found"
