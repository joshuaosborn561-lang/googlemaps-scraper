"""Paid Maps lookups to fill website/domain on ingested businesses."""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .config import ROOT, settings
from .mapsdata import MapsDataClient, domain_of
from .store import Store

_PLANS_DIR = ROOT / "data" / "plans"
_STRIP = re.compile(r"[^a-z0-9]+")


def _norm_name(name: str) -> str:
    return _STRIP.sub(" ", (name or "").lower()).strip()


def _name_score(query_name: str, candidate_name: str) -> float:
    q = set(_norm_name(query_name).split())
    c = set(_norm_name(candidate_name).split())
    if not q or not c:
        return 0.0
    overlap = len(q & c) / max(len(q), 1)
    if _norm_name(query_name) == _norm_name(candidate_name):
        return 1.0
    return overlap


def pending_rows(
    store: Store,
    *,
    source: str = "",
    limit: int | None = None,
    force: bool = False,
) -> list[Any]:
    """Businesses that still need a website/domain from Maps."""
    clauses = ["b.name IS NOT NULL", "b.name != ''"]
    args: list[Any] = []
    if not force:
        clauses.append("(b.domain IS NULL OR b.domain = '')")
    if source:
        clauses.append("COALESCE(NULLIF(b.source,''), 'maps') = ?")
        args.append(source.strip().lower())
    sql = (
        "SELECT b.* FROM businesses b WHERE "
        + " AND ".join(clauses)
        + " ORDER BY b.first_seen DESC, b.place_id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(store.conn.execute(sql, args))


def estimate(
    store: Store,
    *,
    source: str = "",
    limit: int = 0,
) -> dict[str, Any]:
    rows = pending_rows(store, source=source, limit=limit or None, force=False)
    n = len(rows)
    used = store.requests_this_cycle(settings.quota_reset_day)
    overage, billable = settings.plan.cost_for(n, used)
    blocked = overage == float("inf")
    return {
        "source": source or None,
        "businesses_needing_domain": n,
        "requests": n,
        "maps_plan": settings.plan.name,
        "already_used_this_cycle": used,
        "estimated_overage_usd": None if blocked else round(float(overage), 4),
        "blocked": blocked,
        "billable_requests": billable,
        "note": (
            "One paid Maps search per business without a website/domain. "
            "Call resolve_domains(plan_path=…) or resolve_domains() with the latest plan."
        ),
    }


def save_resolve_plan(estimate_payload: dict[str, Any], source: str, limit: int) -> Path:
    _PLANS_DIR.mkdir(parents=True, exist_ok=True)
    path = _PLANS_DIR / f"resolve-domains-{int(time.time())}.json"
    path.write_text(
        json.dumps(
            {
                "kind": "resolve_domains",
                "source": source,
                "limit": limit,
                "estimate": estimate_payload,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _pick_result(biz_name: str, city: str, state: str, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    best, best_score = None, 0.0
    for rec in results:
        if not rec.get("website") and not rec.get("domain"):
            continue
        score = _name_score(biz_name, rec.get("name") or "")
        # Soft boost for same city/state when present.
        if city and (rec.get("city") or "").lower() == city.lower():
            score += 0.15
        if state and (rec.get("state") or "").upper() == state.upper():
            score += 0.1
        if score > best_score:
            best, best_score = rec, score
    if best is None or best_score < 0.5:
        return None
    return best


def resolve_one(
    client: MapsDataClient,
    store: Store,
    row: Any,
) -> dict[str, Any]:
    name = row["name"] or ""
    city = row["city"] or ""
    state = row["state"] or ""
    zip_code = row["zip"] or ""
    zip_row = {
        "zip": zip_code,
        "city": city,
        "state": state,
        "lat": "",
        "lng": "",
    }
    # Prefer city/state in the query; ZIP alone is often missing on ingested rows.
    if city and state:
        client.query_template = "{category} in {city}, {state}"
    elif zip_code:
        client.query_template = "{category} in {zip}"
    else:
        client.query_template = "{category}"

    try:
        results = client.search(name, zip_row)
    except Exception as exc:  # noqa: BLE001
        return {"place_id": row["place_id"], "status": "error", "error": str(exc)[:300]}

    match = _pick_result(name, city, state, results)
    if not match:
        return {"place_id": row["place_id"], "status": "no_match", "candidates": len(results)}

    website = match.get("website") or ""
    domain = match.get("domain") or domain_of(website)
    if not domain:
        return {"place_id": row["place_id"], "status": "no_website", "candidates": len(results)}

    raw = {}
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        raw = {}
    raw["maps_resolve"] = {
        "maps_place_id": match.get("place_id"),
        "maps_name": match.get("name"),
        "maps_url": match.get("maps_url"),
    }
    store.update_business_fields(
        row["place_id"],
        website=website,
        domain=domain,
        maps_url=match.get("maps_url") or row["maps_url"],
        latitude=match.get("latitude") if match.get("latitude") is not None else row["latitude"],
        longitude=match.get("longitude") if match.get("longitude") is not None else row["longitude"],
        address=match.get("address") or row["address"],
        main_category=match.get("main_category") or row["main_category"],
        types=match.get("types") or [],
        raw_json=raw,
    )
    # Move any ext: emails onto the real domain bucket and queue enrich.
    ext_key = f"ext:{row['place_id']}"
    old = [
        r["email"]
        for r in store.conn.execute(
            "SELECT email FROM emails WHERE domain = ?", (ext_key,)
        )
    ]
    if old:
        store.save_emails(domain, old, source=row["source"] or "resolve")
    store.queue_sites()
    return {
        "place_id": row["place_id"],
        "status": "resolved",
        "domain": domain,
        "website": website,
    }


def run(
    store: Store,
    client: MapsDataClient | None = None,
    *,
    source: str = "",
    limit: int = 0,
    force: bool = False,
    workers: int = 4,
) -> dict[str, Any]:
    settings.require_rapidapi()
    rows = pending_rows(store, source=source, limit=limit or None, force=force)
    if not rows:
        return {
            "resolved": 0,
            "no_match": 0,
            "errors": 0,
            "requests": 0,
            "reason": "nothing to resolve: no businesses without a domain match the filters",
        }

    print(f"Resolving domains for {len(rows):,} businesses ({workers} workers)")
    counts = {"resolved": 0, "no_match": 0, "errors": 0, "no_website": 0, "requests": 0}
    lock = __import__("threading").Lock()
    done = 0

    def work(row: Any) -> None:
        nonlocal done
        local_client = MapsDataClient(settings, limit=5)
        result = resolve_one(local_client, store, row)
        with lock:
            counts["requests"] += local_client.request_count
            status = result.get("status") or "errors"
            if status == "resolved":
                counts["resolved"] += 1
            elif status == "error":
                counts["errors"] += 1
            elif status == "no_website":
                counts["no_website"] += 1
            else:
                counts["no_match"] += 1
            done += 1
            if done % 10 == 0 or done == len(rows):
                sys.stderr.write(
                    f"\r  {done:,}/{len(rows):,} | resolved={counts['resolved']:,} "
                    f"no_match={counts['no_match']:,} err={counts['errors']:,}   "
                )
                sys.stderr.flush()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(work, r) for r in rows]
        for f in as_completed(futs):
            f.exception()
    sys.stderr.write("\n")
    counts["source"] = source or None
    return counts
