"""Ingest external lead rows (e.g. Shovels) into the local businesses DB."""

from __future__ import annotations

import json
import re
from typing import Any

from . import emails as email_lib
from .mapsdata import domain_of
from .store import Store

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _s(row: dict[str, Any], *keys: str) -> str:
    for k in keys:
        if k in row and row[k] is not None and str(row[k]).strip():
            return str(row[k]).strip()
    return ""


def _i(row: dict[str, Any], *keys: str) -> int | None:
    for k in keys:
        if k not in row or row[k] in (None, ""):
            continue
        try:
            return int(float(row[k]))
        except (TypeError, ValueError):
            continue
    return None


def _place_id(source_tag: str, external_id: str, name: str, city: str, phone: str) -> str:
    if external_id:
        return f"{source_tag}:{external_id}"
    seed = f"{name}|{city}|{phone}".lower()
    seed = _NON_ALNUM.sub("-", seed).strip("-")
    return f"{source_tag}:syn:{seed[:80]}" if seed else f"{source_tag}:syn:empty"


def normalize_external_row(row: dict[str, Any], source_tag: str) -> dict[str, Any]:
    """Map a Shovels-ish dict into a businesses + owner + emails payload."""
    name = _s(row, "business_name", "company_name", "company", "name")
    # Owner contact name is often just `name` in Shovels exports.
    owner_name = ""
    if _s(row, "business_name", "company_name", "company"):
        owner_name = _s(row, "name", "owner_name", "contact_name", "full_name")
    elif _s(row, "owner_name", "contact_name"):
        owner_name = _s(row, "owner_name", "contact_name")

    website = _s(row, "website", "website_url", "url", "company_website")
    domain = domain_of(website)
    city = _s(row, "address_city", "city")
    state = _s(row, "address_state", "state").upper()
    # Some feeds only give a combined city field; keep state if present.
    if city and "," in city and not state:
        left, _, right = city.partition(",")
        if len(right.strip()) == 2:
            city, state = left.strip(), right.strip().upper()

    address = _s(
        row, "address", "street_address", "address_line1", "full_address"
    )
    zip_code = _s(row, "address_zip", "zip", "postal_code", "zipcode")
    phone = _s(row, "primary_phone", "phone", "phone_number", "mobile")
    external_id = _s(row, "id", "external_id", "shovels_id", "permit_id")
    permit_count = _i(row, "permit_count", "permits", "permit_total")

    primary_raw = _s(row, "primary_email")
    all_raw = _s(row, "email", "emails", "all_emails")
    primary, ranked = email_lib.choose_primary(
        [all_raw, primary_raw],
        website=website,
        site_domain=domain,
        owner_name=owner_name,
        prefer=primary_raw,
    )

    place_id = _place_id(source_tag, external_id, name, city, phone)
    return {
        "place_id": place_id,
        "name": name,
        "address": address,
        "city": city,
        "state": state,
        "zip": zip_code,
        "phone": phone,
        "website": website,
        "domain": domain,
        "source": source_tag,
        "external_id": external_id or None,
        "permit_count": permit_count,
        "raw": row,
        "owner_name": owner_name,
        "email": primary,
        "all_emails": ranked,
    }


def parse_rows(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if isinstance(rows, str):
        text = rows.strip()
        if not text:
            return []
        data = json.loads(text)
    else:
        data = rows
    if isinstance(data, dict):
        # Allow {"rows":[...]} wrappers.
        if "rows" in data and isinstance(data["rows"], list):
            data = data["rows"]
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError("rows must be a JSON list of objects")
    out = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"rows[{i}] is not an object")
        out.append(item)
    return out


def run(
    store: Store,
    rows: Any,
    source_tag: str = "shovels",
    dedupe_on: str = "domain",
) -> dict[str, Any]:
    """Insert external leads. Returns counts only — never echoes rows."""
    tag = (source_tag or "shovels").strip().lower() or "shovels"
    dedupe = (dedupe_on or "domain").strip().lower()
    if dedupe not in ("domain", "external_id", "none", "place_id"):
        raise ValueError("dedupe_on must be domain, external_id, place_id, or none")

    parsed = parse_rows(rows)
    inserted = skipped = updated_owners = emails_saved = 0
    skipped_reasons = {"duplicate_domain": 0, "duplicate_id": 0, "empty_name": 0}

    for raw in parsed:
        rec = normalize_external_row(raw, tag)
        if not rec["name"]:
            skipped += 1
            skipped_reasons["empty_name"] += 1
            continue

        if dedupe == "external_id" and rec.get("external_id"):
            if store.find_by_external_id(tag, str(rec["external_id"])):
                skipped += 1
                skipped_reasons["duplicate_id"] += 1
                continue
        if dedupe == "domain" and rec.get("domain"):
            existing = store.find_by_domain(rec["domain"])
            if existing:
                skipped += 1
                skipped_reasons["duplicate_domain"] += 1
                continue
        if store.get_business(rec["place_id"]):
            skipped += 1
            skipped_reasons["duplicate_id"] += 1
            continue

        if not store.insert_business(rec):
            skipped += 1
            skipped_reasons["duplicate_id"] += 1
            continue
        inserted += 1

        if rec.get("owner_name"):
            store.save_owner(
                rec["place_id"],
                rec["owner_name"],
                None,
                source=tag,
                confidence=0.5,
                model="ingest",
            )
            updated_owners += 1

        addrs = rec.get("all_emails") or []
        if addrs:
            bucket = store.email_bucket(rec["place_id"], rec.get("domain"))
            n = store.save_emails(bucket, addrs, source=tag)
            emails_saved += n

    if inserted:
        store.queue_sites()

    return {
        "source": tag,
        "rows_received": len(parsed),
        "inserted": inserted,
        "skipped": skipped,
        "skipped_reasons": skipped_reasons,
        "owners_saved": updated_owners,
        "emails_saved": emails_saved,
        "dedupe_on": dedupe,
    }
