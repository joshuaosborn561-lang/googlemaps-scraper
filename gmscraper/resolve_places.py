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
from .mapsdata import MapsDataClient, domain_of, parse_address_parts, pick_business_website

# A job that spends more than 3× pending_rows on Maps requests is looping.
REQUESTS_LOOP_MULTIPLIER = 3
# Refuse to start if estimated requests exceed this share of remaining quota.
QUOTA_SHARE_CAP = 0.20


def _cancel_requested(job_id: str = "") -> bool:
    try:
        from mcp_server.jobs import is_cancel_requested

        return bool(is_cancel_requested(job_id))
    except Exception:  # noqa: BLE001
        return False


def _stamp_attempted(binding: sb.SourceBinding, key: Any, at: str) -> None:
    """Best-effort stamp so a failed result patch still excludes the row."""
    if key is None or not at:
        return
    try:
        sb.patch_row(
            binding,
            key,
            {"attempted_at": at, "details_attempted_at": at},
        )
    except Exception:  # noqa: BLE001
        pass


def _details_writeback(details: dict[str, Any] | None) -> dict[str, Any]:
    """Fields to persist from Place Details. Nulls stay in the patch."""
    details = details or {}
    website = (details.get("website") or "").strip()
    if not website:
        website = pick_business_website(details.get("raw") or details)
    phone = (details.get("phone") or "").strip()
    formatted = str(details.get("formatted_phone_number") or "").strip()
    international = str(details.get("international_phone_number") or "").strip()
    if not phone:
        phone = international or formatted
    domain = domain_of(website)
    rating = details.get("rating")
    reviews = details.get("reviews")
    if reviews is None:
        reviews = details.get("user_ratings_total")
    raw_keys = details.get("_raw_keys") or []
    if not raw_keys and isinstance(details.get("raw"), dict):
        raw_keys = sorted(str(k) for k in details["raw"].keys())[:40]
    return {
        "website": website,
        "phone": phone,
        "formatted_phone_number": formatted or None,
        "international_phone_number": international or None,
        "domain": domain,
        "rating": rating,
        "user_ratings_total": reviews,
        "empty_website": not bool(website),
        "http_ok": bool(details.get("_details_http_ok") or details),
        "raw_keys": raw_keys,
    }


def _log_empty_website(key: Any, place_id: str, raw_keys: list[Any]) -> None:
    print(
        f"place_details 200 with no website key={key!r} "
        f"place_id={place_id} raw_keys={raw_keys}",
        flush=True,
    )

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
        # No Place Details call — rejects must not burn a second request.
        patch = {
            binding.resolved_column: True,
            binding.confidence_column: round(conf, 4),
            "candidates": n_cand or len(results),
            "resolved_at": now,
            "attempted_at": now,
            "resolve_raw": raw_store,
        }
        sb.patch_row(binding, key, patch)
        return {
            "key": key,
            "status": "no_match" if not results else "low_confidence",
            "confidence": conf,
            "candidates": n_cand or len(results),
        }

    raw_store["picked"] = {
        "place_id": best.get("place_id"),
        "name": best.get("name"),
        "address": best.get("address"),
        "website": best.get("website"),
    }
    # Never overwrite a stronger existing confidence with a weaker hit.
    # Skip Place Details — we are not writing website/phone on this path.
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
                "attempted_at": now,
                "resolve_raw": raw_store,
            },
        )
        return {
            "key": key,
            "status": "kept_existing",
            "confidence": existing_conf_f,
            "domain": row.get(binding.domain_column),
        }

    # Confidence cleared and we will write — Place Details for website / phone.
    place_id = str(best.get("place_id") or "")
    details = client.place_details(place_id)
    extracted = _details_writeback(details)
    website = extracted["website"]
    phone = extracted["phone"]
    # Fall back to search-hit fields only when details omitted them.
    if not website:
        website = (best.get("website") or "").strip()
        extracted["website"] = website
        extracted["domain"] = domain_of(website)
    if not phone:
        phone = (best.get("phone") or "").strip()
        extracted["phone"] = phone
    domain = extracted["domain"]
    if details and extracted["empty_website"] and not website:
        _log_empty_website(key, place_id, extracted["raw_keys"])
    raw_store["details"] = {
        "place_id": (details or {}).get("place_id") or place_id,
        "website": website,
        "phone": phone,
        "rating": extracted["rating"],
        "user_ratings_total": extracted["user_ratings_total"],
        "empty_website": extracted["empty_website"] and not website,
        "raw_keys": extracted["raw_keys"],
    }
    raw_store["picked"]["website"] = website
    raw_store["picked"]["phone"] = phone
    raw_store["picked"]["domain"] = domain

    patch = {
        binding.domain_column: domain or None,
        binding.confidence_column: round(conf, 4),
        binding.resolved_column: True,
        "website": website or None,
        "phone": phone or None,
        "formatted_phone_number": extracted["formatted_phone_number"],
        "international_phone_number": extracted["international_phone_number"],
        "rating": extracted["rating"],
        "user_ratings_total": extracted["user_ratings_total"],
        "place_id": place_id or None,
        "latitude": best.get("latitude"),
        "longitude": best.get("longitude"),
        "business_name": best.get("name") or None,
        "candidates": n_cand,
        "resolved_at": now,
        "attempted_at": now,
        "details_attempted_at": now,
        "resolve_raw": raw_store,
    }
    sb.patch_row(binding, key, patch)
    return {
        "key": key,
        "status": "resolved",
        "confidence": conf,
        "domain": domain,
        "website": website,
        "phone": phone,
        "place_id": place_id,
        "candidates": n_cand,
        "place_id_found": True,
        "details_attempted": True,
        "details_http_ok": extracted["http_ok"],
        "website_written": bool(website),
        "domain_written": bool(domain),
    }


def details_only_one_row(
    client: MapsDataClient,
    binding: sb.SourceBinding,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Backfill website/phone/domain from an existing place_id (1 Maps request)."""
    key = row.get(binding.key_column)
    place_id = str(row.get("place_id") or "").strip()
    now = datetime.now(timezone.utc).isoformat()
    if not place_id:
        return {"key": key, "status": "no_place_id"}

    details = client.place_details(place_id)
    extracted = _details_writeback(details)
    website = extracted["website"]
    phone = extracted["phone"]
    domain = extracted["domain"]
    if details and extracted["empty_website"]:
        _log_empty_website(key, place_id, extracted["raw_keys"])

    raw_store: dict[str, Any] = {}
    existing_raw = row.get("resolve_raw")
    if isinstance(existing_raw, dict):
        raw_store = dict(existing_raw)
    elif isinstance(existing_raw, str) and existing_raw.strip():
        try:
            parsed = json.loads(existing_raw)
            if isinstance(parsed, dict):
                raw_store = parsed
        except json.JSONDecodeError:
            raw_store = {}
    picked = dict(raw_store.get("picked") or {})
    picked.update(
        {
            "place_id": place_id,
            "website": website,
            "phone": phone,
            "domain": domain,
        }
    )
    raw_store["picked"] = picked
    raw_store["details"] = {
        "place_id": place_id,
        "website": website,
        "phone": phone,
        "rating": extracted["rating"],
        "user_ratings_total": extracted["user_ratings_total"],
        "empty_website": extracted["empty_website"],
        "raw_keys": extracted["raw_keys"],
        "backfill": True,
    }

    patch = {
        binding.domain_column: domain or None,
        binding.resolved_column: True,
        "website": website or None,
        "phone": phone or None,
        "formatted_phone_number": extracted["formatted_phone_number"],
        "international_phone_number": extracted["international_phone_number"],
        "rating": extracted["rating"],
        "user_ratings_total": extracted["user_ratings_total"],
        "place_id": place_id,
        "resolved_at": now,
        "attempted_at": now,
        "details_attempted_at": now,
        "resolve_raw": raw_store,
    }
    # Keep prior confidence / business_name if present.
    if row.get(binding.confidence_column) is not None:
        patch[binding.confidence_column] = row.get(binding.confidence_column)
    if details and details.get("name"):
        patch["business_name"] = details.get("name")
    if details and details.get("latitude") is not None:
        patch["latitude"] = details.get("latitude")
        patch["longitude"] = details.get("longitude")
    sb.patch_row(binding, key, patch)
    return {
        "key": key,
        "status": "details_ok" if (website or phone) else "details_empty",
        "domain": domain,
        "website": website,
        "phone": phone,
        "place_id": place_id,
        "place_id_found": True,
        "details_attempted": True,
        "details_http_ok": extracted["http_ok"],
        "website_written": bool(website),
        "domain_written": bool(domain),
    }


def estimate(
    binding: sb.SourceBinding,
    *,
    limit: int = 0,
    details_only: bool = False,
) -> dict[str, Any]:
    n = sb.count_pending(binding, details_only=details_only)
    if limit and limit > 0:
        n = min(n, int(limit))
    used = 0
    try:
        from .store import Store
        from .config import DEFAULT_DB

        used = Store(str(DEFAULT_DB)).requests_this_cycle(settings.quota_reset_day)
    except Exception:
        used = 0
    # Full resolve: search + details. Backfill: details only.
    per_row = 1 if details_only else 2
    requests = n * per_row
    overage, billable = settings.plan.cost_for(requests, used)
    blocked = overage == float("inf")
    max_cost = float(getattr(settings, "maps_max_cost_usd", 25.0) or 25.0)
    est = None if blocked else round(float(overage), 4)
    cost_blocked = (not blocked) and est is not None and est > max_cost
    remaining = max(0, int(settings.plan.included) - int(used))
    share_cap = int(remaining * QUOTA_SHARE_CAP)
    quota_share_blocked = requests > share_cap
    reason = None
    if blocked:
        reason = "maps_hard_limit"
    elif cost_blocked:
        reason = "exceeds_MAPS_MAX_COST_USD"
    elif quota_share_blocked:
        reason = "exceeds_20pct_remaining_quota"
    return {
        "project_id": binding.project_id,
        "schema": binding.schema,
        "table": binding.table,
        "pending_rows": n,
        "requests": requests,
        "requests_per_row": per_row,
        "details_only": bool(details_only),
        "maps_plan": settings.plan.name,
        "already_used_this_cycle": used,
        "remaining_quota": remaining,
        "quota_share_cap": QUOTA_SHARE_CAP,
        "max_requests_without_override": share_cap,
        "quota_share_blocked": quota_share_blocked,
        "estimated_overage_usd": est,
        "max_cost_usd": max_cost,
        "blocked": blocked or cost_blocked or quota_share_blocked,
        "block_reason": reason,
        "billable_requests": billable,
        "note": (
            "1 Maps request/row (Place Details backfill for place_id without website)."
            if details_only
            else (
                "2 Maps requests/row (search + Place Details). Details only runs when "
                "a candidate clears min_confidence."
            )
        ),
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
    details_only: bool = False,
    project_id: str = "",
    domain_column: str = "domain",
    resolved_column: str = "resolved",
    confidence_column: str = "confidence",
    override_quota_guard: bool = False,
    on_progress: Any | None = None,
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
        est = estimate(binding, limit=limit, details_only=details_only)
        return {
            **est,
            "started": False,
            "estimate_only": True,
            "details_only": bool(details_only),
            "ensured": {},
        }
    ensured = sb.ensure_writeback_columns(binding)
    est = estimate(binding, limit=limit, details_only=details_only)
    if est.get("blocked"):
        if not (
            est.get("block_reason") == "exceeds_20pct_remaining_quota"
            and override_quota_guard
        ):
            return {
                **est,
                "started": False,
                "blocked": True,
                "ensured": ensured,
            }
        est = {**est, "blocked": False, "quota_guard_overridden": True}

    settings.require_rapidapi()
    job_id = ""
    try:
        from mcp_server.jobs import current_job_id

        job_id = current_job_id() or ""
    except Exception:  # noqa: BLE001
        job_id = ""
    batch_size = max(25, min(200, int(workers or 8) * 25))
    pending_total = int(est.get("pending_rows") or 0)
    if limit and limit > 0:
        pending_total = min(pending_total, int(limit)) if pending_total else int(limit)
    run_started = datetime.now(timezone.utc).isoformat()
    request_cap = REQUESTS_LOOP_MULTIPLIER * pending_total if pending_total else 0
    counts = {
        "started": True,
        "rows": 0,
        "resolved": 0,
        "place_id_found": 0,
        "details_attempted": 0,
        "details_ok": 0,
        "website_written": 0,
        "domain_written": 0,
        "details_empty": 0,
        "low_confidence": 0,
        "no_match": 0,
        "kept_existing": 0,
        "errors": 0,
        "requests": 0,
        "last_error": None,
        "error_samples": [],
        "details_only": bool(details_only),
        "estimated_overage_usd": est.get("estimated_overage_usd"),
        "batch_size": batch_size,
        "run_started_at": run_started,
        "request_cap": request_cap,
        "stop_reason": None,
        "quota_guard_overridden": bool(est.get("quota_guard_overridden")),
    }
    lock = threading.Lock()
    stop_event = threading.Event()
    seen_keys: set[Any] = set()
    done = 0
    # Freeze total at pending_rows. Never raise it to match done — that hid the loop.
    total = pending_total or 0
    max_rows = int(limit) if limit and limit > 0 else 0

    def _record_error(key: Any, exc: BaseException) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        if len(msg) > 400:
            msg = msg[:397] + "..."
        sample = {"key": key, "error": msg}
        samples = counts["error_samples"]
        if isinstance(samples, list) and len(samples) < 25:
            samples.append(sample)
        counts["last_error"] = msg

    def _should_stop() -> bool:
        if stop_event.is_set() or _cancel_requested(job_id):
            return True
        if request_cap and counts["requests"] > request_cap:
            return True
        if total and done >= total:
            return True
        return False

    def _mark_stop(reason: str) -> None:
        if not counts.get("stop_reason"):
            counts["stop_reason"] = reason
        if reason == "cancelled":
            counts["cancelled"] = True
        stop_event.set()

    def _tick(**extra: Any) -> None:
        if not on_progress:
            return
        try:
            on_progress(
                stage="resolve_places",
                done=done,
                total=total,
                resolved=counts["resolved"],
                place_id_found=counts["place_id_found"],
                details_attempted=counts["details_attempted"],
                details_ok=counts["details_ok"],
                website_written=counts["website_written"],
                domain_written=counts["domain_written"],
                no_match=counts["no_match"],
                errors=counts["errors"],
                last_error=counts.get("last_error"),
                error_samples=list(counts.get("error_samples") or []),
                requests=counts["requests"],
                request_cap=request_cap,
                stop_reason=counts.get("stop_reason"),
                project_id=binding.project_id,
                table=binding.table,
                **extra,
            )
        except Exception:  # noqa: BLE001
            pass

    _tick(batch=0)

    def work(row: dict[str, Any]) -> None:
        nonlocal done
        key = row.get(binding.key_column)
        if stop_event.is_set() or _cancel_requested(job_id):
            _mark_stop(
                "cancelled"
                if _cancel_requested(job_id)
                else (counts.get("stop_reason") or "stopped")
            )
            return
        stamp = datetime.now(timezone.utc).isoformat()
        _stamp_attempted(binding, key, stamp)
        local = MapsDataClient(settings, limit=8)
        err_exc: BaseException | None = None
        status = "errors"
        try:
            if stop_event.is_set() or _cancel_requested(job_id):
                _mark_stop("cancelled")
                return
            if details_only:
                result = details_only_one_row(local, binding, row)
            else:
                result = resolve_one_row(
                    local,
                    binding,
                    row,
                    strategy=strategy,
                    min_confidence=float(min_confidence),
                )
            status = result.get("status") or "errors"
            if status == "errors" and not result.get("error"):
                err_exc = RuntimeError("row returned status=errors")
        except Exception as exc:  # noqa: BLE001
            status = "errors"
            result = {}
            err_exc = exc
        with lock:
            counts["requests"] += local.request_count
            if result.get("place_id") or result.get("place_id_found"):
                counts["place_id_found"] += 1
            if result.get("details_attempted"):
                counts["details_attempted"] += 1
            if result.get("website_written") or result.get("website"):
                counts["website_written"] += 1
            if result.get("domain_written") or result.get("domain"):
                counts["domain_written"] += 1
            if result.get("details_http_ok") or status == "details_ok":
                counts["details_ok"] += 1
            if status == "resolved":
                counts["resolved"] += 1
            elif status == "details_ok":
                counts["resolved"] += 1
            elif status == "details_empty":
                counts["details_empty"] += 1
            elif status == "low_confidence":
                counts["low_confidence"] += 1
            elif status == "kept_existing":
                counts["kept_existing"] += 1
            elif status == "no_match":
                counts["no_match"] += 1
            else:
                counts["errors"] += 1
                _record_error(
                    key,
                    err_exc or RuntimeError(f"unknown status {status!r}"),
                )
            done += 1
            if request_cap and counts["requests"] > request_cap:
                _mark_stop("requests_exceed_3x_pending")
            if total and done > total:
                _mark_stop("done_exceeds_total")
            if err_exc is not None or done <= 20 or done % 10 == 0 or (
                total and done >= total
            ) or stop_event.is_set():
                _tick()

    batch_n = 0
    while True:
        if _cancel_requested(job_id):
            _mark_stop("cancelled")
            break
        if stop_event.is_set():
            break
        if request_cap and counts["requests"] > request_cap:
            _mark_stop("requests_exceed_3x_pending")
            break
        if total and done >= total:
            break

        remaining_cap = 0
        if max_rows:
            remaining_cap = max_rows - done
            if remaining_cap <= 0:
                break
        fetch_lim = batch_size if not remaining_cap else min(batch_size, remaining_cap)
        rows = sb.fetch_pending(
            binding,
            limit=fetch_lim,
            details_only=bool(details_only),
            attempted_since=run_started,
        )
        if not rows:
            break
        fresh = []
        for r in rows:
            key = r.get(binding.key_column)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            fresh.append(r)
        if not fresh:
            _mark_stop("repeat_keys")
            break
        if not total:
            total = pending_total or (len(fresh) if max_rows else len(fresh))
        batch_n += 1
        _tick(batch=batch_n, batch_rows=len(fresh))
        with ThreadPoolExecutor(max_workers=max(1, int(workers or 8))) as pool:
            futs = []
            for r in fresh:
                if _should_stop():
                    if _cancel_requested(job_id):
                        _mark_stop("cancelled")
                    break
                futs.append(pool.submit(work, r))
            for f in as_completed(futs):
                f.exception()
                if _cancel_requested(job_id):
                    _mark_stop("cancelled")
                if stop_event.is_set():
                    break
        if counts.get("cancelled") or stop_event.is_set():
            break
        if len(fresh) < fetch_lim:
            break

    counts["rows"] = done
    _tick(batch=batch_n, finished=True)

    return {
        **counts,
        "project_id": binding.project_id,
        "schema": binding.schema,
        "table": binding.table,
        "strategy": strategy,
        "min_confidence": min_confidence,
        "ensured": ensured,
        "pending_rows": pending_total,
    }
