"""Final stage: CSV out (and shared lead row builders for sample/sync)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterator, Literal

from . import emails as email_lib
from .zips import haversine_miles, parse_center

COLUMNS = [
    "place_id", "name", "owner_name", "owner_title", "owner_source",
    "email", "all_emails", "phone", "website", "domain",
    "address", "city", "state", "zip", "source_zip",
    "rating", "reviews", "main_category", "types", "latitude", "longitude",
    "maps_url", "in_icp", "icp_confidence", "icp_reason", "source_category",
]

SAMPLE_COLUMNS = [
    "name", "domain", "email", "all_emails", "city", "main_category",
    "in_icp", "icp_confidence", "icp_reason", "owner_name", "owner_title",
]

BASE_SQL = """
SELECT b.place_id, b.name, b.phone, b.website, b.domain, b.address, b.city,
       b.state, b.zip, b.source_zip, b.rating, b.reviews, b.main_category, b.types,
       b.latitude, b.longitude, b.maps_url, b.source_category,
       v.in_icp, v.confidence AS icp_confidence, v.reason AS icp_reason,
       o.owner_name, o.owner_title, o.source AS owner_source
FROM businesses b
LEFT JOIN verdicts v ON v.place_id = b.place_id
LEFT JOIN owners   o ON o.place_id = b.place_id
"""


def _build_where(
    *,
    icp_only: bool = True,
    with_owner: bool = False,
    with_phone: bool = False,
    with_website: bool = False,
    with_email: bool = False,
    min_confidence: float = 0.0,
    min_rating: float = 0.0,
    min_reviews: int = 0,
    states: list[str] | None = None,
    city: str | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if icp_only:
        clauses.append("v.in_icp = 1")
    if min_confidence > 0:
        clauses.append("COALESCE(v.confidence, 0) >= ?")
        args.append(min_confidence)
    if with_owner:
        clauses.append("o.owner_name IS NOT NULL AND o.owner_name != ''")
    if with_phone:
        clauses.append("b.phone IS NOT NULL AND b.phone != ''")
    if with_website or with_email:
        clauses.append("b.domain IS NOT NULL AND b.domain != ''")
    if with_email:
        clauses.append("EXISTS (SELECT 1 FROM emails e WHERE e.domain = b.domain)")
    if min_rating > 0:
        clauses.append("COALESCE(b.rating, 0) >= ?")
        args.append(min_rating)
    if min_reviews > 0:
        clauses.append("COALESCE(b.reviews, 0) >= ?")
        args.append(min_reviews)
    if states:
        clauses.append(f"b.state IN ({','.join('?' * len(states))})")
        args.extend(s.upper() for s in states)
    if city:
        clauses.append("LOWER(b.city) = LOWER(?)")
        args.append(city.strip())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, args


def _radius_tuple(
    center: str | None,
    radius_miles: float | None,
    center_lat: float | None,
    center_lng: float | None,
) -> tuple[float, float, float] | None:
    if not radius_miles or radius_miles <= 0:
        return None
    if center_lat is not None and center_lng is not None:
        return (float(center_lat), float(center_lng), float(radius_miles))
    if center:
        lat, lng, _ = parse_center(center)
        return (lat, lng, float(radius_miles))
    return None


def _emails_for_row(store, rec: dict[str, Any], by_domain: dict[str, list[str]]) -> list[str]:
    domain = (rec.get("domain") or "").strip().lower()
    place_id = rec.get("place_id") or ""
    pool: list[str] = []
    seen: set[str] = set()
    for key in ((domain,) if domain else ()) + ((f"ext:{place_id}",) if place_id else ()):
        for e in by_domain.get(key, []):
            if e not in seen:
                seen.add(e)
                pool.append(e)
    # Fallback for DBs that gained emails after by_domain was built.
    if not pool and hasattr(store, "emails_for_business"):
        pool = store.emails_for_business(place_id, domain)
    return pool


def _normalize_row(
    rec: dict[str, Any],
    by_domain: dict[str, list[str]],
    store=None,
) -> dict[str, Any]:
    out = dict(rec)
    try:
        out["types"] = ", ".join(json.loads(out.get("types") or "[]"))
    except (json.JSONDecodeError, TypeError):
        out["types"] = out.get("types") or ""
    if out.get("in_icp") is not None:
        out["in_icp"] = "yes" if out["in_icp"] else "no"

    ranked = email_lib.rank(
        _emails_for_row(store, out, by_domain),
        out.get("domain") or "",
        out.get("owner_name") or "",
    )
    out["email"] = ranked[0] if ranked else ""
    # Full multi-value list (Shovels cells can hold 15+ addresses).
    out["all_emails"] = ", ".join(ranked)
    return out


def iter_leads(
    store,
    *,
    icp_only: bool = True,
    with_owner: bool = False,
    with_phone: bool = False,
    with_website: bool = False,
    with_email: bool = False,
    min_confidence: float = 0.0,
    min_rating: float = 0.0,
    min_reviews: int = 0,
    states: list[str] | None = None,
    city: str | None = None,
    center: str | None = None,
    radius_miles: float | None = None,
    center_lat: float | None = None,
    center_lng: float | None = None,
    order: Literal["name", "recent", "random"] = "name",
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield export-shaped lead dicts from the local SQLite store."""
    where, args = _build_where(
        icp_only=icp_only,
        with_owner=with_owner,
        with_phone=with_phone,
        with_website=with_website,
        with_email=with_email,
        min_confidence=min_confidence,
        min_rating=min_rating,
        min_reviews=min_reviews,
        states=states,
        city=city,
    )
    sql = BASE_SQL + where
    if order == "recent":
        sql += " ORDER BY COALESCE(b.first_seen, '') DESC, b.place_id DESC"
    elif order == "random":
        sql += " ORDER BY RANDOM()"
    else:
        sql += " ORDER BY b.state, b.city, b.name"

    # For random/recent samples, push LIMIT into SQL when no radius filter.
    radius_filter = _radius_tuple(center, radius_miles, center_lat, center_lng)
    if limit and radius_filter is None:
        sql += f" LIMIT {int(limit)}"

    by_domain = store.emails_by_domain()
    n = 0
    for row in store.conn.execute(sql, args):
        rec = dict(row)
        if radius_filter is not None:
            try:
                blat = float(rec.get("latitude") or 0)
                blng = float(rec.get("longitude") or 0)
            except (TypeError, ValueError):
                continue
            if not blat and not blng:
                continue
            clat, clng, miles = radius_filter
            if haversine_miles(clat, clng, blat, blng) > miles:
                continue
        out = _normalize_row(rec, by_domain, store=store)
        yield out
        n += 1
        if limit and n >= limit:
            break


def fetch_leads(store, **kwargs: Any) -> list[dict[str, Any]]:
    return list(iter_leads(store, **kwargs))


def sample_leads(
    store,
    *,
    limit: int = 20,
    icp_only: bool = False,
    with_email: bool = False,
    city: str = "",
    order: Literal["random", "recent"] = "random",
) -> list[dict[str, Any]]:
    """Compact row subset for QA. Random by default to avoid ZIP-prefix bias."""
    lim = max(1, min(int(limit or 20), 100))
    rows = fetch_leads(
        store,
        icp_only=icp_only,
        with_email=with_email,
        city=city or None,
        order=order if order in ("random", "recent") else "random",
        limit=lim,
    )
    return [{k: r.get(k, "") for k in SAMPLE_COLUMNS} for r in rows]


def run(
    store,
    out_path: str | Path,
    icp_only: bool = True,
    with_owner: bool = False,
    with_phone: bool = False,
    with_website: bool = False,
    with_email: bool = False,
    min_confidence: float = 0.0,
    min_rating: float = 0.0,
    min_reviews: int = 0,
    states: list[str] | None = None,
    center: str | None = None,
    radius_miles: float | None = None,
    center_lat: float | None = None,
    center_lng: float | None = None,
) -> int:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for rec in iter_leads(
            store,
            icp_only=icp_only,
            with_owner=with_owner,
            with_phone=with_phone,
            with_website=with_website,
            with_email=with_email,
            min_confidence=min_confidence,
            min_rating=min_rating,
            min_reviews=min_reviews,
            states=states,
            center=center,
            radius_miles=radius_miles,
            center_lat=center_lat,
            center_lng=center_lng,
            order="name",
        ):
            w.writerow(rec)
            n += 1
    return n
