"""Sync parcel rows from scrape_leads → permit_parcel.parcels with pagination.

Fixes:
- Cursor pagination (no silent 50k cap)
- Honour county filter
- Upsert on natural key (county, account_id)
"""

from __future__ import annotations

import json
from typing import Any
from urllib import error, parse, request

from . import source_binding as sb

PAGE_SIZE = 1000


def _headers(url_key: dict[str, str]) -> dict[str, str]:
    return {
        "apikey": url_key["key"],
        "Authorization": f"Bearer {url_key['key']}",
        "Content-Type": "application/json",
        "Prefer": "count=exact",
    }


def _get_json(creds: dict[str, str], path: str) -> tuple[list[Any], dict[str, str]]:
    url = f"{creds['url']}/rest/v1/{path}"
    req = request.Request(url, headers=_headers(creds), method="GET")
    try:
        with request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return json.loads(body or "[]"), headers
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GET {url} failed ({exc.code}): {detail[:400]}"
        ) from exc


def _row_from_scrape_lead(lead: dict[str, Any]) -> dict[str, Any] | None:
    raw = lead.get("raw") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    county = (
        raw.get("county")
        or _county_from_tags(lead.get("tags"))
        or ""
    )
    account_id = str(raw.get("account_id") or "").strip()
    if not county or not account_id:
        return None
    county = str(county).strip()
    return {
        "id": f"{county}:{account_id}",
        "county": county,
        "account_id": account_id,
        "owner_name": raw.get("owner_name") or raw.get("name") or lead.get("name") or "",
        "mailing_address": raw.get("mailing_address"),
        "parcel_address": raw.get("address") or raw.get("parcel_address") or lead.get("name"),
        "city": raw.get("city") or lead.get("city"),
        "zip": raw.get("zip") or lead.get("zip"),
        "assessed_value": raw.get("assessed_value"),
        "use_code": raw.get("use_code") or raw.get("category") or lead.get("category"),
        "prop_type": raw.get("prop_type"),
        "owner_type": raw.get("owner_type") or "unknown",
    }


def _county_from_tags(tags: Any) -> str:
    if not tags:
        return ""
    if isinstance(tags, str):
        tags = [tags]
    mapping = {
        "tarrant": "Tarrant",
        "dallas": "Dallas",
        "collin": "Collin",
        "denton": "Denton",
    }
    for t in tags:
        key = str(t).strip().lower()
        if key in mapping:
            return mapping[key]
        # bare county name
        if key.capitalize() in ("Tarrant", "Dallas", "Collin", "Denton"):
            return key.capitalize()
    return ""


def sync_parcels(
    *,
    county: str = "",
    project_id: str = "",
    page_size: int = PAGE_SIZE,
    max_pages: int = 0,
    cursor: int = 0,
) -> dict[str, Any]:
    """Page through scrape_leads tagged parcels and upsert into permit_parcel.parcels.

    Returns counts + resume cursor. Never returns row payloads.
    """
    creds = sb.resolve_credentials(project_id or sb._env("LEADS_SUPABASE_PROJECT_ID"))
    binding = sb.SourceBinding(
        project_id=creds["project_id"],
        schema="public",
        table="scrape_leads",
        key_column="id",
        address_column="name",
        supabase_url=creds["url"],
        supabase_key=creds["key"],
    )

    county_norm = (county or "").strip()
    offset = max(0, int(cursor or 0))
    page = max(1, int(page_size or PAGE_SIZE))
    upserted = 0
    skipped = 0
    pages = 0
    has_more = True
    last_offset = offset

    while has_more:
        if max_pages and pages >= max_pages:
            break
        # Prefer SECURITY DEFINER RPC — scrape_leads is RLS-protected for anon.
        safe_county = county_norm.replace("'", "")
        where = "tags @> ARRAY['parcels']::text[]"
        if safe_county:
            where += (
                f" AND (raw->>'county' ILIKE '{safe_county}' "
                f"OR '{safe_county.lower()}' = ANY(tags))"
            )
        batch = sb.rpc(
            binding,
            "pp_select_rows",
            {
                "p_schema": "public",
                "p_table": "scrape_leads",
                "p_columns": ["id", "tags", "name", "city", "zip", "category", "raw"],
                "p_where": where,
                "p_order_by": "id",
                "p_limit": page,
                "p_offset": offset,
            },
        )
        headers: dict[str, str] = {}
        if not isinstance(batch, list):
            batch = []

        if not batch:
            has_more = False
            break

        rows = []
        for lead in batch:
            if not isinstance(lead, dict):
                skipped += 1
                continue
            mapped = _row_from_scrape_lead(lead)
            if not mapped:
                skipped += 1
                continue
            if county_norm and mapped["county"].lower() != county_norm.lower():
                skipped += 1
                continue
            rows.append(mapped)

        if rows:
            n = sb.rpc(binding, "pp_upsert_parcels", {"rows": rows})
            upserted += int(n or len(rows))

        pages += 1
        offset += len(batch)
        last_offset = offset
        # Content-Range: 0-999/12345
        cr = headers.get("content-range") or headers.get("content-range".title()) or ""
        total = None
        if "/" in cr:
            try:
                total = int(cr.split("/")[-1])
            except ValueError:
                total = None
        if total is not None:
            has_more = offset < total
        else:
            has_more = len(batch) >= page

    return {
        "dataset": "parcels",
        "county": county_norm or None,
        "project_id": binding.project_id,
        "pages": pages,
        "rows_upserted": upserted,
        "rows_skipped": skipped,
        "cursor": last_offset,
        "has_more": has_more,
        "resume_token": str(last_offset),
        "page_size": page,
    }
