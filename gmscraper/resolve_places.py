"""Address-/name-first Maps resolution against a generic Supabase source table."""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from . import source_binding as sb
from .config import settings
from .mapsdata import MapsDataClient, domain_of, parse_address_parts

STREET_NUM = re.compile(r"^\s*(\d+[A-Za-z]?)\b")
SUITE = re.compile(
    r"\b(?:ste|suite|unit|apt|apartment|#)\s*([A-Za-z0-9\-]+)\b", re.I
)
STREET_NAME = re.compile(
    r"^\s*\d+[A-Za-z]?\s+(.+?)(?:\s*,|\s+(?:ste|suite|unit|apt|#)\b|$)", re.I
)

VIRTUAL_OFFICE = re.compile(
    r"\b(wework|regus|industrious|spaces\b|virtual\s+office|registered\s+agent|"
    r"legalzoom|northwest\s+registered|corporation\s+service|csc\s+global|"
    r"cowork|co-work|shared\s+office)\b",
    re.I,
)


def _street_number(addr: str) -> str:
    m = STREET_NUM.search(addr or "")
    return (m.group(1) if m else "").upper()


def _street_name(addr: str) -> str:
    m = STREET_NAME.search(addr or "")
    if not m:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", m.group(1).lower()).strip()


def _suite(addr: str) -> str:
    m = SUITE.search(addr or "")
    return (m.group(1) if m else "").upper()


def _zip(addr: str, explicit: str = "") -> str:
    if explicit and re.match(r"^\d{5}", explicit.strip()):
        return explicit.strip()[:5]
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", addr or "")
    return m.group(1) if m else ""


def score_candidate(query_address: str, result: dict[str, Any]) -> float:
    """0..1 confidence that result is the right business for the query address."""
    q_addr = query_address or ""
    r_addr = result.get("address") or ""
    score = 0.35  # base for any Maps hit

    qn, rn = _street_number(q_addr), _street_number(r_addr)
    if qn and rn:
        score += 0.30 if qn == rn else -0.35
    qname, rname = _street_name(q_addr), _street_name(r_addr)
    if qname and rname:
        score += 0.20 if qname == rname or qname in rname or rname in qname else -0.15

    qz = _zip(q_addr)
    rz = (result.get("zip") or _zip(r_addr))[:5]
    if qz and rz:
        score += 0.10 if qz == rz else -0.05

    qs, rs = _suite(q_addr), _suite(r_addr)
    if qs and rs and qs != rs:
        score -= 0.35
    elif qs and not rs:
        # Query had a suite; result is whole building — likely wrong tenant.
        score -= 0.25

    blob = f"{result.get('name') or ''} {result.get('main_category') or ''}"
    if VIRTUAL_OFFICE.search(blob):
        score -= 0.55

    return max(0.0, min(1.0, score))


def pick_best(
    query_address: str,
    results: list[dict[str, Any]],
    *,
    min_confidence: float,
) -> tuple[dict[str, Any] | None, float, int]:
    if not results:
        return None, 0.0, 0
    scored = [(score_candidate(query_address, r), r) for r in results]
    scored.sort(key=lambda x: -x[0])
    best_score, best = scored[0]
    # Cap when many distinct businesses share the address (multi-tenant).
    distinct = {
        (r.get("place_id") or r.get("name") or "").lower() for _, r in scored
    }
    n = len([d for d in distinct if d])
    if n >= 4:
        best_score = min(best_score, 0.45)
    elif n >= 2:
        best_score = min(best_score, 0.55)
    if best_score < min_confidence:
        return None, best_score, n
    return best, best_score, n


def _zip_row_from_address(address: str, city: str = "") -> dict[str, str]:
    c, state, zipc = parse_address_parts(address or "")
    return {
        "zip": zipc or "",
        "city": city or c or "",
        "state": state or "",
        "lat": "",
        "lng": "",
    }


def _search(
    client: MapsDataClient,
    query: str,
    zip_row: dict[str, str],
) -> list[dict[str, Any]]:
    client.query_template = "{category}"
    return client.search(query, zip_row)


def resolve_one_row(
    client: MapsDataClient,
    binding: sb.SourceBinding,
    row: dict[str, Any],
    *,
    strategy: str,
    min_confidence: float,
) -> dict[str, Any]:
    key = row.get(binding.key_column)
    address = str(row.get(binding.address_column) or "").strip() if binding.address_column else ""
    name = str(row.get(binding.name_column) or "").strip() if binding.name_column else ""
    city = str(row.get(binding.city_column) or "").strip() if binding.city_column else ""
    zip_row = _zip_row_from_address(address, city)

    results: list[dict[str, Any]] = []
    used_query = ""
    strat = (strategy or "address").strip().lower()

    if strat in ("address", "address_then_name") and address:
        used_query = address
        results = _search(client, address, zip_row)
    if (not results and strat in ("name", "address_then_name")) and name:
        q = f"{name} {city}".strip() if city else name
        used_query = q
        results = _search(client, q, zip_row)
    if not results and strat == "address" and not address and name:
        used_query = name
        results = _search(client, name, zip_row)

    best, conf, n_cand = pick_best(address or used_query, results, min_confidence=min_confidence)
    now = datetime.now(timezone.utc).isoformat()
    raw_store = {
        "query": used_query,
        "strategy": strat,
        "candidates": [
            {
                "place_id": r.get("place_id"),
                "name": r.get("name"),
                "address": r.get("address"),
                "website": r.get("website"),
                "score": score_candidate(address or used_query, r),
            }
            for r in results[:10]
        ],
        "picked": None,
        "confidence": conf,
    }

    if not best:
        # Persist miss / low-confidence for review; mark resolved so we don't re-spend.
        patch = {
            binding.resolved_column: True,
            binding.confidence_column: round(conf, 4),
            "candidates": n_cand or len(results),
            "resolved_at": now,
            "resolve_raw": raw_store,
        }
        sb.patch_row(binding, key, patch)
        return {
            "key": key,
            "status": "no_match" if not results else "low_confidence",
            "confidence": conf,
            "candidates": n_cand or len(results),
        }

    website = best.get("website") or ""
    domain = best.get("domain") or domain_of(website)
    raw_store["picked"] = {
        "place_id": best.get("place_id"),
        "name": best.get("name"),
        "address": best.get("address"),
        "website": website,
    }
    # Never overwrite a stronger existing confidence with a weaker hit.
    existing_conf = row.get(binding.confidence_column)
    try:
        existing_conf_f = float(existing_conf) if existing_conf is not None else None
    except (TypeError, ValueError):
        existing_conf_f = None
    if existing_conf_f is not None and existing_conf_f >= conf and row.get(binding.domain_column):
        sb.patch_row(
            binding,
            key,
            {
                binding.resolved_column: True,
                "candidates": n_cand,
                "resolved_at": now,
                "resolve_raw": raw_store,
            },
        )
        return {
            "key": key,
            "status": "kept_existing",
            "confidence": existing_conf_f,
            "domain": row.get(binding.domain_column),
        }

    patch = {
        binding.domain_column: domain or None,
        binding.confidence_column: round(conf, 4),
        binding.resolved_column: True,
        "website": website or None,
        "phone": best.get("phone") or None,
        "place_id": best.get("place_id") or None,
        "latitude": best.get("latitude"),
        "longitude": best.get("longitude"),
        "business_name": best.get("name") or None,
        "candidates": n_cand,
        "resolved_at": now,
        "resolve_raw": raw_store,
    }
    sb.patch_row(binding, key, patch)
    return {
        "key": key,
        "status": "resolved",
        "confidence": conf,
        "domain": domain,
        "candidates": n_cand,
    }


def estimate(
    binding: sb.SourceBinding,
    *,
    limit: int = 0,
) -> dict[str, Any]:
    n = sb.count_pending(binding)
    if limit and limit > 0:
        n = min(n, int(limit))
    used = 0
    try:
        from .store import Store
        from .config import DEFAULT_DB

        used = Store(str(DEFAULT_DB)).requests_this_cycle(settings.quota_reset_day)
    except Exception:
        used = 0
    overage, billable = settings.plan.cost_for(n, used)
    blocked = overage == float("inf")
    max_cost = float(getattr(settings, "maps_max_cost_usd", 25.0) or 25.0)
    est = None if blocked else round(float(overage), 4)
    cost_blocked = (not blocked) and est is not None and est > max_cost
    return {
        "project_id": binding.project_id,
        "schema": binding.schema,
        "table": binding.table,
        "pending_rows": n,
        "requests": n,
        "maps_plan": settings.plan.name,
        "already_used_this_cycle": used,
        "estimated_overage_usd": est,
        "max_cost_usd": max_cost,
        "blocked": blocked or cost_blocked,
        "block_reason": (
            "maps_hard_limit"
            if blocked
            else ("exceeds_MAPS_MAX_COST_USD" if cost_blocked else None)
        ),
        "billable_requests": billable,
    }


def run(
    *,
    schema: str,
    table: str,
    key_column: str,
    address_column: str = "",
    name_column: str = "",
    city_column: str = "",
    where: str = "",
    order_by: str = "",
    limit: int = 0,
    strategy: str = "address",
    min_confidence: float = 0.6,
    workers: int = 8,
    estimate_only: bool = False,
    project_id: str = "",
    domain_column: str = "domain",
    resolved_column: str = "resolved",
    confidence_column: str = "confidence",
) -> dict[str, Any]:
    binding = sb.resolve_binding(
        project_id=project_id,
        schema=schema,
        table=table,
        key_column=key_column,
        address_column=address_column,
        name_column=name_column,
        city_column=city_column,
        where=where,
        order_by=order_by,
        domain_column=domain_column,
        resolved_column=resolved_column,
        confidence_column=confidence_column,
    )
    sb.validate_binding(binding)
    # Estimates are read-only — never ALTER TABLE / ensure columns.
    if estimate_only:
        est = estimate(binding, limit=limit)
        return {**est, "started": False, "estimate_only": True, "ensured": {}}
    ensured = sb.ensure_writeback_columns(binding)
    est = estimate(binding, limit=limit)
    if est.get("blocked"):
        return {
            **est,
            "started": False,
            "blocked": True,
            "ensured": ensured,
        }

    settings.require_rapidapi()
    rows = sb.fetch_pending(binding, limit=limit or 0)
    counts = {
        "started": True,
        "rows": len(rows),
        "resolved": 0,
        "low_confidence": 0,
        "no_match": 0,
        "kept_existing": 0,
        "errors": 0,
        "requests": 0,
        "estimated_overage_usd": est.get("estimated_overage_usd"),
    }
    lock = threading.Lock()

    def work(row: dict[str, Any]) -> None:
        local = MapsDataClient(settings, limit=8)
        try:
            result = resolve_one_row(
                local,
                binding,
                row,
                strategy=strategy,
                min_confidence=float(min_confidence),
            )
            status = result.get("status") or "errors"
        except Exception:  # noqa: BLE001
            status = "errors"
            result = {}
        with lock:
            counts["requests"] += local.request_count
            if status == "resolved":
                counts["resolved"] += 1
            elif status == "low_confidence":
                counts["low_confidence"] += 1
            elif status == "kept_existing":
                counts["kept_existing"] += 1
            elif status == "no_match":
                counts["no_match"] += 1
            else:
                counts["errors"] += 1

    with ThreadPoolExecutor(max_workers=max(1, int(workers or 8))) as pool:
        futs = [pool.submit(work, r) for r in rows]
        for f in as_completed(futs):
            f.exception()

    return {
        **counts,
        "project_id": binding.project_id,
        "schema": binding.schema,
        "table": binding.table,
        "strategy": strategy,
        "min_confidence": min_confidence,
        "ensured": ensured,
    }
