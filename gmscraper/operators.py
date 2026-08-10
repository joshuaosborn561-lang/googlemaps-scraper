"""Build permit_parcel.operators from parcels with in-state + geo filters.

Aggregates parcels by mailing_address, drops out-of-state mailings, optionally
keeps only parcels whose ZIP centroid falls inside center+radius, and replaces
the operators table via ``replace_permit_parcel_operators``.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any
from urllib import error, request

from . import address_state
from . import source_binding as sb
from . import zips as zips_mod

PAGE_SIZE = 2000


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _normalize_address(addr: str) -> str:
    return " ".join((addr or "").upper().split())


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _local_llc(owner_name: str, states: set[str]) -> bool:
    """Heuristic: LLC/LP name mentions an allowed state code."""
    blob = (owner_name or "").upper()
    if not blob:
        return False
    for st in states:
        if f" {st} " in f" {blob} " or blob.endswith(f" {st}") or f"{st} " in blob[:4]:
            if any(x in blob for x in (" LLC", " LP", " INC", " LTD", " CORP")):
                return True
    return False


def _resolve_radius_zips(
    *,
    center: str,
    radius_miles: float,
    allowed_states: set[str],
) -> tuple[set[str], dict[str, Any]]:
    """ZIP codes whose centroids fall inside the radius (optionally state-limited)."""
    lat, lng, label = zips_mod.parse_center(center)
    rows = zips_mod.within_radius(
        lat,
        lng,
        float(radius_miles),
        states=sorted(allowed_states) if allowed_states else None,
    )
    zset = {r["zip"] for r in rows if r.get("zip")}
    meta = {
        "center": label,
        "center_lat": lat,
        "center_lng": lng,
        "radius_miles": float(radius_miles),
        "zips_in_radius": len(zset),
    }
    return zset, meta


def _aggregate_parcels(
    parcels: list[dict[str, Any]],
    *,
    allowed_states: set[str],
    zip_allow: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Group parcels by mailing address; keep in-state (+ optional ZIP radius)."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    stats = {
        "parcels_read": 0,
        "parcels_no_mailing": 0,
        "parcels_oos": 0,
        "parcels_outside_radius": 0,
        "parcels_kept": 0,
        "operators": 0,
    }
    for p in parcels:
        stats["parcels_read"] += 1
        mailing = str(p.get("mailing_address") or "").strip()
        if not mailing:
            stats["parcels_no_mailing"] += 1
            continue
        if zip_allow is not None:
            pz = str(p.get("zip") or "").strip()[:5]
            if not pz or pz not in zip_allow:
                stats["parcels_outside_radius"] += 1
                continue
        if not address_state.is_in_states(mailing, allowed_states):
            stats["parcels_oos"] += 1
            continue
        stats["parcels_kept"] += 1
        groups[_normalize_address(mailing)].append(p)

    rows: list[dict[str, Any]] = []
    for norm, items in groups.items():
        operator_address = str(items[0].get("mailing_address") or norm).strip()
        llcs: dict[str, int] = defaultdict(int)
        counties: set[str] = set()
        total_value = 0
        largest = 0
        top_parcel = ""
        has_local = False
        for it in items:
            owner = str(it.get("owner_name") or "").strip()
            if owner:
                llcs[owner] += 1
            county = str(it.get("county") or "").strip()
            if county:
                counties.add(county)
            val = _as_int(it.get("assessed_value"), 0)
            total_value += val
            if val >= largest:
                largest = val
                top_parcel = str(it.get("parcel_address") or "").strip()
            if _local_llc(owner, allowed_states):
                has_local = True
        top_llc = ""
        if llcs:
            top_llc = sorted(llcs.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        rows.append(
            {
                "operator_address": operator_address,
                "parcels": len(items),
                "distinct_llcs": len(llcs),
                "portfolio_value": total_value,
                "largest_parcel_value": largest,
                "counties": len(counties),
                "county_list": sorted(counties),
                "top_llc": top_llc or None,
                "top_parcel_address": top_parcel or None,
                "has_local_llc": has_local,
                "operator_name": None,
                "domain": None,
                "website": None,
                "phone": None,
                "place_id": None,
                "confidence": None,
                "resolved": False,
                "resolved_at": None,
            }
        )
    rows.sort(key=lambda r: (-int(r["portfolio_value"]), r["operator_address"]))
    stats["operators"] = len(rows)
    return rows, stats


def _fetch_all_parcels(binding: sb.SourceBinding) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    cols = [
        "id",
        "county",
        "owner_name",
        "mailing_address",
        "parcel_address",
        "assessed_value",
        "zip",
        "city",
    ]
    while True:
        batch = sb.rpc(
            binding,
            "pp_select_rows",
            {
                "p_schema": "permit_parcel",
                "p_table": "parcels",
                "p_columns": cols,
                "p_where": "mailing_address IS NOT NULL AND mailing_address <> ''",
                "p_order_by": "id",
                "p_limit": PAGE_SIZE,
                "p_offset": offset,
            },
        )
        if not isinstance(batch, list) or not batch:
            break
        out.extend(b for b in batch if isinstance(b, dict))
        if len(batch) < PAGE_SIZE:
            break
        offset += len(batch)
    return out


def _replace_operators(
    binding: sb.SourceBinding,
    rows: list[dict[str, Any]],
    *,
    secret: str,
) -> dict[str, Any]:
    url = f"{binding.supabase_url}/rest/v1/rpc/replace_permit_parcel_operators"
    body = json.dumps({"p_secret": secret, "p_rows": rows}).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={
            "apikey": binding.supabase_key,
            "Authorization": f"Bearer {binding.supabase_key}",
            "Content-Type": "application/json",
            "Content-Profile": "public",
            "Accept-Profile": "public",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=300) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"replace_permit_parcel_operators failed ({exc.code}): {detail[:500]}"
        ) from exc
    if not text:
        return {"ok": True, "operators_built": len(rows)}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ok": True, "raw": text[:200], "operators_built": len(rows)}


def build_operators(
    *,
    states: str = "TX",
    project_id: str = "",
    dry_run: bool = False,
    min_parcels: int = 1,
    center: str = "",
    radius_miles: float = 0.0,
) -> dict[str, Any]:
    """Rebuild permit_parcel.operators from parcels.

    states: comma-separated USPS codes for mailing filter (default TX).
    center + radius_miles: keep only parcels whose ZIP is inside the radius
    (market geography). Mailing can still be elsewhere in ``states``.
    dry_run: aggregate + report counts without truncating operators.
    """
    allowed = {s.strip().upper() for s in (states or "TX").split(",") if s.strip()}
    if not allowed:
        raise ValueError("states= is required (e.g. states='TX')")
    min_n = max(1, _as_int(min_parcels, 1))

    creds = sb.resolve_credentials(
        project_id or _env("LEADS_SUPABASE_PROJECT_ID", "kemvxzhcxvynmoutwdrh")
    )
    binding = sb.SourceBinding(
        project_id=creds["project_id"],
        schema="permit_parcel",
        table="parcels",
        key_column="id",
        address_column="mailing_address",
        supabase_url=creds["url"],
        supabase_key=creds["key"],
    )

    geo_meta: dict[str, Any] = {}
    zip_allow: set[str] | None = None
    if (center or "").strip() and float(radius_miles or 0) > 0:
        zip_allow, geo_meta = _resolve_radius_zips(
            center=center.strip(),
            radius_miles=float(radius_miles),
            allowed_states=allowed,
        )
        if not zip_allow:
            raise ValueError(
                f"No ZIPs found within {radius_miles} mi of {center!r} "
                f"for states={sorted(allowed)}"
            )

    parcels = _fetch_all_parcels(binding)
    rows, stats = _aggregate_parcels(
        parcels, allowed_states=allowed, zip_allow=zip_allow
    )
    if min_n > 1:
        before = len(rows)
        rows = [r for r in rows if _as_int(r.get("parcels"), 0) >= min_n]
        stats["operators_below_min_parcels"] = before - len(rows)
        stats["operators"] = len(rows)

    # Sample of what would be excluded (from current operators table) for QA.
    oos_examples: list[str] = []
    try:
        sample = sb.rpc(
            binding,
            "pp_select_rows",
            {
                "p_schema": "permit_parcel",
                "p_table": "operators",
                "p_columns": ["operator_address", "portfolio_value"],
                "p_where": None,
                "p_order_by": "portfolio_value DESC NULLS LAST",
                "p_limit": 200,
                "p_offset": 0,
            },
        )
        for r in sample or []:
            addr = str((r or {}).get("operator_address") or "")
            if addr and not address_state.is_in_states(addr, allowed):
                oos_examples.append(addr)
                if len(oos_examples) >= 10:
                    break
    except Exception:  # noqa: BLE001
        pass

    top_sample = [
        {
            "operator_address": r["operator_address"],
            "parcels": r["parcels"],
            "portfolio_value": r["portfolio_value"],
            "top_llc": r.get("top_llc"),
            "counties": r.get("counties"),
        }
        for r in rows[:15]
    ]

    out: dict[str, Any] = {
        "project_id": binding.project_id,
        "states": sorted(allowed),
        "dry_run": bool(dry_run),
        "min_parcels": min_n,
        **stats,
        "geo": geo_meta or None,
        "oos_examples_in_current_top": oos_examples,
        "top_operators_sample": top_sample,
        "note": (
            "Mailing must parse to allowed states. Optional center+radius filters "
            "by parcel ZIP centroid (buildings in market), not mailing city."
        ),
    }
    if dry_run:
        out["started"] = False
        out["operators_built"] = 0
        return out

    secret = _env("SUPABASE_INGEST_SECRET") or _env("LEADS_SUPABASE_INGEST_SECRET")
    if not secret:
        raise RuntimeError(
            "SUPABASE_INGEST_SECRET is required to replace operators "
            "(calls replace_permit_parcel_operators)."
        )
    result = _replace_operators(binding, rows, secret=secret)
    out["started"] = True
    out["operators_built"] = int(
        result.get("operators_built") or result.get("inserted") or len(rows)
    )
    out["replace_result"] = {
        k: result.get(k) for k in ("ok", "operators_built", "inserted") if k in result
    }
    return out
