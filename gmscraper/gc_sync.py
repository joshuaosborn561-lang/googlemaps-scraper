"""Write enrichment results directly to Supabase gc.companies / gc.contacts."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

SCHEMA = "gc"
COMPANIES = "companies"
CONTACTS = "contacts"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def supabase_config() -> dict[str, str]:
    url = _env("SUPABASE_URL").rstrip("/")
    key = _env("SUPABASE_SERVICE_ROLE_KEY") or _env("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError(
            "Supabase not configured. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_ANON_KEY)."
        )
    return {"url": url, "key": key}


def _headers(key: str, *, prefer: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
        "Accept-Profile": SCHEMA,
        "Content-Profile": SCHEMA,
    }


def _request(
    method: str,
    path: str,
    key: str,
    base_url: str,
    *,
    body: Any = None,
    prefer: str = "return=minimal",
) -> tuple[int, str]:
    url = f"{base_url}/rest/v1/{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers=_headers(key, prefer=prefer),
        method=method,
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase {method} {url} failed ({exc.code}): {detail[:500]}"
        ) from exc


def upsert_companies(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    cfg = supabase_config()
    # PostgREST upsert on domain PK
    _request(
        "POST",
        f"{COMPANIES}?on_conflict=domain",
        cfg["key"],
        cfg["url"],
        body=rows,
        prefer="resolution=merge-duplicates,return=minimal",
    )
    return len(rows)


def insert_contacts(rows: list[dict[str, Any]]) -> int:
    """Insert contact rows (used for null-email rows with no unique conflict)."""
    if not rows:
        return 0
    cfg = supabase_config()
    _request(
        "POST",
        CONTACTS,
        cfg["key"],
        cfg["url"],
        body=rows,
        prefer="return=minimal",
    )
    return len(rows)


def insert_contacts_ignore_conflict(rows: list[dict[str, Any]]) -> int:
    """Insert contacts; skip duplicates on unique (domain, email).

    Rows with a null/empty email must NOT use this path — Postgres unique
    indexes treat NULLs as distinct, but the conflict target requires email.
    Use insert_contacts() for null-email rows instead.
    """
    if not rows:
        return 0
    # Drop empties so on_conflict=domain,email is well-defined.
    clean = [r for r in rows if (r.get("email") or "").strip()]
    if not clean:
        return 0
    cfg = supabase_config()
    _request(
        "POST",
        f"{CONTACTS}?on_conflict=domain,email",
        cfg["key"],
        cfg["url"],
        body=clean,
        prefer="resolution=ignore-duplicates,return=minimal",
    )
    return len(clean)


def _job_level(title: str) -> str:
    t = (title or "").lower()
    if any(k in t for k in ("ceo", "owner", "founder", "president", "principal", "partner")):
        return "C-Team"
    if "vp" in t or "vice president" in t:
        return "VP"
    if "director" in t:
        return "Director"
    if "manager" in t:
        return "Manager"
    return ""


def company_row(
    *,
    domain: str,
    company_name: str = "",
    source: str = "maps",
    in_maps_icp: bool = False,
    in_shovels: bool = False,
    permit_count: int | None = None,
    place: str = "",
    address_city: str = "",
    address_state: str = "",
    email_source_tier: str = "",
    dm_source_tier: str = "",
    source_tier: dict[str, str] | None = None,
    website: str = "",
    shovels_email: str = "",
    shovels_name: str = "",
    dm_lookup_status: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    tier = source_tier or {}
    if email_source_tier:
        tier = {**tier, "email": email_source_tier}
    if dm_source_tier:
        tier = {**tier, "dm": dm_source_tier}
    return {
        "domain": domain,
        "company_name": company_name or None,
        "source": source or None,
        "in_maps_icp": bool(in_maps_icp),
        "in_shovels": bool(in_shovels),
        "permit_count": permit_count,
        "place": place or None,
        "address_city": address_city or None,
        "address_state": address_state or None,
        "email_source_tier": email_source_tier or None,
        "dm_source_tier": dm_source_tier or None,
        "source_tier": tier,
        "website": website or (f"https://{domain}" if domain else None),
        "shovels_email": shovels_email or None,
        "shovels_name": shovels_name or None,
        "dm_lookup_status": dm_lookup_status or None,
        "updated_at": now,
    }


def contact_row(
    *,
    domain: str,
    first_name: str = "",
    last_name: str = "",
    job_title: str = "",
    email: str = "",
    email_status: str = "",
    cellphone: str = "",
    linkedin_url: str = "",
    contact_city: str = "",
    contact_state: str = "",
    source_tool: str = "",
    source_tier: str = "",
    source_url: str = "",
    confidence: float | None = None,
    place_id: str = "",
    job_level: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    title = job_title or ""
    return {
        "domain": domain,
        "first_name": first_name or None,
        "last_name": last_name or None,
        "job_title": title or None,
        "job_level": job_level or _job_level(title) or None,
        "email": (email or "").lower() or None,
        "email_status": email_status or None,
        "cellphone": cellphone or None,
        "linkedin_url": linkedin_url or None,
        "contact_city": contact_city or None,
        "contact_state": contact_state or None,
        "source_tool": source_tool or source_tier or None,
        "source_tier": source_tier or source_tool or None,
        "source_url": source_url or None,
        "confidence": confidence,
        "place_id": place_id or None,
        "updated_at": now,
    }
