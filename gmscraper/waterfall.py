"""Email / DM enrichment waterfall.

Order (fixed): getleads → AI Ark → LeadMagic → FullEnrich (last).

- AI Ark is people discovery only (never email-to-profile reverse lookup).
- FullEnrich is email-only and runs only after the first three miss.
- Results write to Supabase gc.companies / gc.contacts (not MCP response body).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from . import gc_sync
from .store import Store
from .vendors.ai_ark import AiArkClient
from .vendors.base import EmailHit, PersonHit, split_name
from .vendors.fullenrich import FullEnrichClient
from .vendors.getleads import GetLeadsClient
from .vendors.leadmagic import LeadMagicClient

Need = Literal["email", "dm", "both"]

DM_TITLE_HINTS = (
    "owner", "founder", "principal", "president", "ceo", "partner",
    "director", "vp", "vice president", "managing",
)


def _is_dm_title(title: str) -> bool:
    t = (title or "").lower()
    return any(h in t for h in DM_TITLE_HINTS)


def _parse_rows(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if isinstance(rows, str):
        rows = json.loads(rows) if rows.strip() else []
    if not isinstance(rows, list):
        raise ValueError("rows must be a JSON list of objects")
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append(r)
    return out


def _norm_row(r: dict[str, Any]) -> dict[str, Any]:
    domain = (
        r.get("domain")
        or r.get("website")
        or ""
    )
    if isinstance(domain, str) and "://" in domain:
        from urllib.parse import urlsplit
        host = urlsplit(domain if "://" in domain else f"https://{domain}").hostname or ""
        domain = host[4:] if host.startswith("www.") else host
    domain = str(domain or "").strip().lower()
    first = str(r.get("first_name") or "").strip()
    last = str(r.get("last_name") or "").strip()
    full = str(r.get("name") or r.get("full_name") or r.get("owner_name") or "").strip()
    if not first and full:
        first, last = split_name(full)
    return {
        "domain": domain,
        "company_name": str(r.get("company_name") or r.get("company") or r.get("business_name") or "").strip(),
        "first_name": first,
        "last_name": last,
        "full_name": full or f"{first} {last}".strip(),
        "title": str(r.get("title") or r.get("job_title") or r.get("owner_title") or "").strip(),
        "email": str(r.get("email") or "").strip().lower(),
        "place_id": str(r.get("place_id") or "").strip(),
        "city": str(r.get("city") or r.get("address_city") or "").strip(),
        "state": str(r.get("state") or r.get("address_state") or "").strip(),
        "in_maps_icp": bool(r.get("in_maps_icp") or r.get("in_icp") in (True, 1, "yes", "1")),
        "in_shovels": bool(r.get("in_shovels")),
        "permit_count": r.get("permit_count"),
        "source": str(r.get("source") or "maps"),
        "linkedin_url": str(r.get("linkedin_url") or "").strip(),
    }


class Waterfall:
    def __init__(
        self,
        *,
        getleads: GetLeadsClient | None = None,
        ai_ark: AiArkClient | None = None,
        leadmagic: LeadMagicClient | None = None,
        fullenrich: FullEnrichClient | None = None,
        store: Store | None = None,
    ):
        self.getleads = getleads or GetLeadsClient()
        self.ai_ark = ai_ark or AiArkClient()
        self.leadmagic = leadmagic or LeadMagicClient()
        self.fullenrich = fullenrich or FullEnrichClient()
        self.store = store
        self.tier_stats = {
            "getleads": {"calls": 0, "email_hits": 0, "dm_hits": 0},
            "ai_ark": {"calls": 0, "email_hits": 0, "dm_hits": 0},
            "leadmagic": {"calls": 0, "email_hits": 0, "dm_hits": 0},
            "fullenrich": {"calls": 0, "email_hits": 0, "dm_hits": 0},
            "team_page": {"calls": 0, "email_hits": 0, "dm_hits": 0},
        }

    def _bump(self, tier: str, field: str) -> None:
        self.tier_stats.setdefault(tier, {"calls": 0, "email_hits": 0, "dm_hits": 0})
        self.tier_stats[tier][field] = self.tier_stats[tier].get(field, 0) + 1

    def resolve_email(self, row: dict[str, Any]) -> EmailHit | None:
        """getleads → LeadMagic → FullEnrich. AI Ark skipped for email."""
        first, last, domain = row["first_name"], row["last_name"], row["domain"]
        if row.get("email"):
            return EmailHit(email=row["email"], source_tier="input", status="provided")
        if not (first and last and domain):
            return None

        if self.getleads.enabled:
            self._bump("getleads", "calls")
            hit = self.getleads.find_email(first, last, domain, row.get("company_name") or "")
            if hit:
                self._bump("getleads", "email_hits")
                return hit

        if self.leadmagic.enabled:
            self._bump("leadmagic", "calls")
            hit = self.leadmagic.find_email(first, last, domain, row.get("company_name") or "")
            if hit:
                self._bump("leadmagic", "email_hits")
                return hit

        if self.fullenrich.enabled:
            self._bump("fullenrich", "calls")
            hit = self.fullenrich.find_email(first, last, domain, row.get("company_name") or "")
            if hit:
                self._bump("fullenrich", "email_hits")
                return hit
        return None

    def resolve_dm(self, row: dict[str, Any]) -> PersonHit | None:
        """Discover a decision-maker: local team_page → getleads → AI Ark → LeadMagic."""
        domain = row["domain"]
        if not domain:
            return None

        # Free local signal first (not a paid tier, but fills DMs).
        if self.store:
            self._bump("team_page", "calls")
            contacts = self.store.contacts_for_domain(domain)
            for c in contacts:
                if _is_dm_title(c.get("title") or ""):
                    first, last = split_name(c.get("name") or "")
                    self._bump("team_page", "dm_hits")
                    return PersonHit(
                        first_name=first,
                        last_name=last,
                        full_name=c.get("name") or "",
                        title=c.get("title") or "",
                        email=c.get("email") or "",
                        source_tier="team_page",
                        raw=dict(c),
                    )

        if row.get("full_name") and _is_dm_title(row.get("title") or "owner"):
            return PersonHit(
                first_name=row["first_name"],
                last_name=row["last_name"],
                full_name=row["full_name"],
                title=row.get("title") or "",
                email=row.get("email") or "",
                source_tier="input",
            )

        if self.getleads.enabled:
            self._bump("getleads", "calls")
            people = self.getleads.find_people(domain, row.get("company_name") or "")
            for p in people:
                if _is_dm_title(p.title) or not p.title:
                    self._bump("getleads", "dm_hits")
                    return p

        if self.ai_ark.enabled:
            self._bump("ai_ark", "calls")
            people = self.ai_ark.find_people(domain, company_name=row.get("company_name") or "")
            for p in people:
                if _is_dm_title(p.title) or True:
                    self._bump("ai_ark", "dm_hits")
                    return p

        if self.leadmagic.enabled:
            self._bump("leadmagic", "calls")
            people = self.leadmagic.find_people(domain, row.get("company_name") or "")
            for p in people:
                self._bump("leadmagic", "dm_hits")
                return p
        return None


def enrich_waterfall(
    rows: Any,
    *,
    need: Need = "both",
    store: Store | None = None,
    write_supabase: bool = True,
) -> dict[str, Any]:
    """Walk tiers per row; upsert companies/contacts to Supabase; return counts only."""
    parsed = [_norm_row(r) for r in _parse_rows(rows)]
    parsed = [r for r in parsed if r.get("domain")]
    if not parsed:
        return {
            "rows_in": 0,
            "companies_upserted": 0,
            "contacts_written": 0,
            "emails_found": 0,
            "dms_found": 0,
            "tier_stats": {},
            "need": need,
        }

    wf = Waterfall(store=store)
    company_rows: list[dict[str, Any]] = []
    contact_rows: list[dict[str, Any]] = []
    emails_found = 0
    dms_found = 0

    # Bulk FullEnrich pass for rows that still need email after earlier tiers.
    pending_fe: list[tuple[int, dict[str, Any]]] = []

    enriched: list[dict[str, Any]] = []
    for idx, row in enumerate(parsed):
        email_tier = ""
        dm_tier = ""
        email = row.get("email") or ""
        person: PersonHit | None = None

        if need in ("dm", "both"):
            person = wf.resolve_dm(row)
            if person:
                dms_found += 1
                dm_tier = person.source_tier
                if not row["first_name"] and person.first_name:
                    row["first_name"] = person.first_name
                    row["last_name"] = person.last_name
                    row["full_name"] = person.name
                if not row.get("title") and person.title:
                    row["title"] = person.title
                if person.email and not email:
                    email = person.email
                    email_tier = person.source_tier

        if need in ("email", "both") and not email:
            # Inline getleads + leadmagic; defer fullenrich to bulk.
            first, last, domain = row["first_name"], row["last_name"], row["domain"]
            if first and last and domain:
                if wf.getleads.enabled:
                    wf._bump("getleads", "calls")
                    hit = wf.getleads.find_email(
                        first, last, domain, row.get("company_name") or ""
                    )
                    if hit:
                        wf._bump("getleads", "email_hits")
                        email, email_tier = hit.email, hit.source_tier
                if not email and wf.leadmagic.enabled:
                    wf._bump("leadmagic", "calls")
                    hit = wf.leadmagic.find_email(
                        first, last, domain, row.get("company_name") or ""
                    )
                    if hit:
                        wf._bump("leadmagic", "email_hits")
                        email, email_tier = hit.email, hit.source_tier
                if not email and wf.fullenrich.enabled:
                    pending_fe.append((idx, row))

        enriched.append(
            {
                "row": row,
                "email": email,
                "email_tier": email_tier,
                "dm_tier": dm_tier,
                "person": person,
            }
        )

    # FullEnrich bulk for remaining email misses.
    if pending_fe and wf.fullenrich.enabled:
        fe_rows = [
            {
                "first_name": r["first_name"],
                "last_name": r["last_name"],
                "domain": r["domain"],
                "company_name": r.get("company_name") or "",
            }
            for _, r in pending_fe
        ]
        wf._bump("fullenrich", "calls")
        hits = wf.fullenrich.find_email_bulk(fe_rows)
        for (idx, _row), hit in zip(pending_fe, hits):
            if hit:
                wf._bump("fullenrich", "email_hits")
                enriched[idx]["email"] = hit.email
                enriched[idx]["email_tier"] = hit.source_tier

    for item in enriched:
        row = item["row"]
        email = item["email"]
        email_tier = item["email_tier"]
        dm_tier = item["dm_tier"]
        person = item["person"]
        if email:
            emails_found += 1

        company_rows.append(
            gc_sync.company_row(
                domain=row["domain"],
                company_name=row.get("company_name") or "",
                source=row.get("source") or "maps",
                in_maps_icp=bool(row.get("in_maps_icp")),
                in_shovels=bool(row.get("in_shovels")),
                permit_count=row.get("permit_count")
                if isinstance(row.get("permit_count"), int)
                else None,
                place=row.get("place_id") or "",
                address_city=row.get("city") or "",
                address_state=row.get("state") or "",
                email_source_tier=email_tier,
                dm_source_tier=dm_tier,
                source_tier={
                    k: v
                    for k, v in {"email": email_tier, "dm": dm_tier}.items()
                    if v
                },
                dm_lookup_status="found" if dm_tier else "not_found",
            )
        )

        first = row["first_name"]
        last = row["last_name"]
        if person:
            first = person.first_name or first
            last = person.last_name or last
        if first or last or email:
            contact_rows.append(
                gc_sync.contact_row(
                    domain=row["domain"],
                    first_name=first,
                    last_name=last,
                    job_title=row.get("title") or (person.title if person else ""),
                    email=email,
                    email_status="found" if email else "",
                    linkedin_url=(person.linkedin_url if person else "")
                    or row.get("linkedin_url")
                    or "",
                    contact_city=row.get("city") or "",
                    contact_state=row.get("state") or "",
                    source_tool=email_tier or dm_tier or "waterfall",
                    source_tier=email_tier or dm_tier,
                    place_id=row.get("place_id") or "",
                    confidence=0.7 if email or dm_tier else 0.0,
                )
            )

        # Mirror into local SQLite when available.
        if store and (first or last):
            store.save_contact(
                name=f"{first} {last}".strip(),
                domain=row["domain"],
                place_id=row.get("place_id") or "",
                title=row.get("title") or (person.title if person else ""),
                email=email,
                source=email_tier or dm_tier or "waterfall",
                source_tier=email_tier or dm_tier,
                confidence=0.7 if email else 0.5,
            )
            if email:
                store.save_emails(row["domain"], [email], source=email_tier or "waterfall")

    companies_upserted = 0
    contacts_written = 0
    if write_supabase:
        companies_upserted = gc_sync.upsert_companies(company_rows)
        contacts_written = gc_sync.insert_contacts(contact_rows)

    # Merge live client counters into tier_stats for reporting.
    for name, client in (
        ("getleads", wf.getleads),
        ("ai_ark", wf.ai_ark),
        ("leadmagic", wf.leadmagic),
        ("fullenrich", wf.fullenrich),
    ):
        wf.tier_stats[name]["vendor_calls"] = getattr(client, "calls", 0)
        wf.tier_stats[name]["vendor_hits"] = getattr(client, "hits", 0)

    return {
        "rows_in": len(parsed),
        "companies_upserted": companies_upserted,
        "contacts_written": contacts_written,
        "emails_found": emails_found,
        "dms_found": dms_found,
        "tier_stats": wf.tier_stats,
        "need": need,
        "vendors_enabled": {
            "getleads": wf.getleads.enabled,
            "ai_ark": wf.ai_ark.enabled,
            "leadmagic": wf.leadmagic.enabled,
            "fullenrich": wf.fullenrich.enabled,
        },
    }
