"""Lead export / query / summary for MCP clients (CSV text in-response)."""

from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path
from typing import Any, Iterator, Literal

from . import emails as email_lib
from .mapsdata import parse_address_parts
from .store import MissingClientTag, normalize_client_tag
from .zips import haversine_miles, parse_center

COLUMNS = [
    "place_id", "name", "owner_name", "owner_title", "owner_source",
    "email", "all_emails", "phone", "website", "domain",
    "address", "city", "state", "zip", "source_zip",
    "rating", "reviews", "main_category", "types", "latitude", "longitude",
    "maps_url", "in_icp", "icp_confidence", "icp_reason", "source_category",
    "permit_count", "source",
]

# Default columns returned to MCP clients (icp_reason opt-in).
CLIENT_COLUMNS = [
    "place_id", "name", "domain", "email", "all_emails", "phone", "city",
    "state", "main_category", "in_icp", "icp_confidence", "owner_name",
    "owner_title", "source_zip", "latitude", "longitude",
]

SAMPLE_COLUMNS = [
    "name", "domain", "email", "all_emails", "city", "main_category",
    "in_icp", "icp_confidence", "icp_reason", "owner_name", "owner_title",
]

EXPORT_CAP = 5000
QUERY_PAGE_MAX = 50

_BASE_SELECT = """
SELECT b.place_id, b.name, b.phone, b.website, b.domain, b.address, b.city,
       b.state, b.zip, b.source_zip, b.rating, b.reviews, b.main_category, b.types,
       b.latitude, b.longitude, b.maps_url, b.source_category,
       b.permit_count, b.source AS lead_source,
       {icp_cols},
       o.owner_name, o.owner_title, o.source AS owner_source
FROM businesses b
{icp_join}
LEFT JOIN owners   o ON o.place_id = b.place_id
"""


def _base_sql(client_tag: str) -> tuple[str, list[Any]]:
    """Join business_icp for one client. No tag → ICP columns are NULL."""
    if client_tag:
        sql = _BASE_SELECT.format(
            icp_cols=(
                "v.in_icp, v.confidence AS icp_confidence, "
                "v.reason AS icp_reason, v.client_tag AS icp_client_tag"
            ),
            icp_join=(
                "LEFT JOIN business_icp v "
                "ON v.place_id = b.place_id AND v.client_tag = ?"
            ),
        )
        return sql, [client_tag]
    sql = _BASE_SELECT.format(
        icp_cols=(
            "NULL AS in_icp, NULL AS icp_confidence, "
            "NULL AS icp_reason, NULL AS icp_client_tag"
        ),
        icp_join="",
    )
    return sql, []


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
    min_permits: int = 0,
    states: list[str] | None = None,
    city: str | None = None,
    state: str | None = None,
    q: str | None = None,
    source: str | None = None,
    client_tag: str = "",
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if icp_only:
        if not client_tag:
            raise MissingClientTag(
                "icp_only=true requires client_tag — in_icp is per client, "
                "not a global flag. Pass client_tag='basco' or "
                "client_tag='peterson'."
            )
        clauses.append("v.in_icp = 1")
    if min_confidence > 0:
        clauses.append("COALESCE(v.confidence, 0) >= ?")
        args.append(min_confidence)
    if with_owner:
        clauses.append("o.owner_name IS NOT NULL AND o.owner_name != ''")
    if with_phone:
        clauses.append("b.phone IS NOT NULL AND b.phone != ''")
    if with_website:
        clauses.append("b.domain IS NOT NULL AND b.domain != ''")
    if with_email:
        clauses.append(
            """(
                EXISTS (
                  SELECT 1 FROM emails e
                  WHERE e.domain = b.domain AND b.domain IS NOT NULL AND b.domain != ''
                )
                OR EXISTS (
                  SELECT 1 FROM emails e WHERE e.domain = 'ext:' || b.place_id
                )
            )"""
        )
    if min_rating > 0:
        clauses.append("COALESCE(b.rating, 0) >= ?")
        args.append(min_rating)
    if min_reviews > 0:
        clauses.append("COALESCE(b.reviews, 0) >= ?")
        args.append(min_reviews)
    if min_permits > 0:
        clauses.append("COALESCE(b.permit_count, 0) >= ?")
        args.append(int(min_permits))
    if states:
        clauses.append(f"b.state IN ({','.join('?' * len(states))})")
        args.extend(s.upper() for s in states)
    if state:
        clauses.append("UPPER(b.state) = ?")
        args.append(state.strip().upper())
    if city:
        clauses.append("LOWER(b.city) = LOWER(?)")
        args.append(city.strip())
    if source:
        clauses.append("COALESCE(NULLIF(b.source,''), 'maps') = ?")
        args.append(source.strip().lower())
    if q:
        needle = f"%{q.strip().lower()}%"
        clauses.append(
            """(
                LOWER(COALESCE(b.name,'')) LIKE ?
                OR LOWER(COALESCE(b.domain,'')) LIKE ?
                OR LOWER(COALESCE(b.city,'')) LIKE ?
                OR LOWER(COALESCE(o.owner_name,'')) LIKE ?
            )"""
        )
        args.extend([needle, needle, needle, needle])
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


def _city_from_zip(zip_code: str) -> tuple[str, str]:
    """Return (city, state) for a 5-digit ZIP via the offline zipcodes table."""
    z = (zip_code or "").strip()[:5]
    if not z.isdigit() or len(z) != 5:
        return "", ""
    try:
        import zipcodes
    except ImportError:  # pragma: no cover
        return "", ""
    matches = zipcodes.matching(z) or []
    if not matches:
        return "", ""
    m = matches[0]
    return (m.get("city") or "").strip(), (m.get("state") or "").strip().upper()


def backfill_blank_cities(store) -> int:
    """Fill empty city (and state/zip when missing) from address or source_zip.

    ~10% of Maps rows have a blank city (often also blank address). Prefer parsing
    the formatted address; fall back to the scrape source_zip / zip via the
    offline USPS ZIP table so geo filters still work.
    """
    rows = list(
        store.conn.execute(
            "SELECT place_id, address, city, state, zip, source_zip FROM businesses "
            "WHERE city IS NULL OR city = ''"
        )
    )
    n = 0
    for row in rows:
        city, state, zip_code = parse_address_parts(row["address"] or "")
        if not city:
            city, state_from_zip = _city_from_zip(
                (row["zip"] or "") or (row["source_zip"] or "")
            )
            if state_from_zip and not state:
                state = state_from_zip
            if not zip_code:
                zip_code = ((row["zip"] or "") or (row["source_zip"] or "")).strip()[:5]
        if not city:
            continue
        fields: dict[str, Any] = {"city": city}
        if not (row["state"] or "").strip() and state:
            fields["state"] = state
        if not (row["zip"] or "").strip() and zip_code:
            fields["zip"] = zip_code
        store.update_business_fields(row["place_id"], **fields)
        n += 1
    return n


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
    if not pool and hasattr(store, "emails_for_business"):
        pool = store.emails_for_business(place_id, domain)
    return pool


def _normalize_row(
    rec: dict[str, Any],
    by_domain: dict[str, list[str]],
    store=None,
    *,
    clean: bool = False,
) -> dict[str, Any] | None:
    """Return an export-shaped row, or None when clean=True and every email is junk."""
    out = dict(rec)
    if "lead_source" in out and "source" not in out:
        out["source"] = out.get("lead_source") or "maps"
    try:
        out["types"] = ", ".join(json.loads(out.get("types") or "[]"))
    except (json.JSONDecodeError, TypeError):
        out["types"] = out.get("types") or ""
    if out.get("in_icp") is not None:
        out["in_icp"] = "yes" if out["in_icp"] else "no"

    domain = out.get("domain") or ""
    pool = _emails_for_row(store, out, by_domain)
    had_emails = bool(pool)
    if clean:
        pool = [e for e in pool if email_lib.is_clean_lead_email(e, domain)]
        if had_emails and not pool:
            return None
    ranked = email_lib.rank(pool, domain, out.get("owner_name") or "")
    out["email"] = ranked[0] if ranked else ""
    out["all_emails"] = "|".join(ranked)
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
    min_permits: int = 0,
    states: list[str] | None = None,
    city: str | None = None,
    state: str | None = None,
    q: str | None = None,
    source: str | None = None,
    center: str | None = None,
    radius_miles: float | None = None,
    center_lat: float | None = None,
    center_lng: float | None = None,
    order: Literal["name", "recent", "random"] = "name",
    limit: int | None = None,
    offset: int = 0,
    clean: bool = False,
    client_tag: str = "",
) -> Iterator[dict[str, Any]]:
    """Yield export-shaped lead dicts from the local SQLite store."""
    tag = normalize_client_tag(client_tag)
    if icp_only and not tag:
        raise MissingClientTag(
            "icp_only=true requires client_tag — in_icp is per client, "
            "not a global flag. Pass client_tag='basco' or "
            "client_tag='peterson'."
        )
    where, args = _build_where(
        icp_only=icp_only,
        with_owner=with_owner,
        with_phone=with_phone,
        with_website=with_website,
        with_email=with_email,
        min_confidence=min_confidence,
        min_rating=min_rating,
        min_reviews=min_reviews,
        min_permits=min_permits,
        states=states,
        city=city,
        state=state,
        q=q,
        source=source,
        client_tag=tag,
    )
    base_sql, join_args = _base_sql(tag)
    sql = base_sql + where
    args = join_args + args
    if order == "recent":
        sql += " ORDER BY COALESCE(b.first_seen, '') DESC, b.place_id DESC"
    elif order == "random":
        sql += " ORDER BY RANDOM()"
    else:
        sql += " ORDER BY b.state, b.city, b.name"

    radius_filter = _radius_tuple(center, radius_miles, center_lat, center_lng)
    # When cleaning/radius filtering post-hoc, over-fetch then filter.
    sql_limit = None
    if limit and radius_filter is None and not clean:
        sql_limit = int(limit) + int(offset or 0)
        sql += f" LIMIT {sql_limit}"
        if offset and not clean:
            # SQLite OFFSET only with LIMIT
            pass

    by_domain = store.emails_by_domain()
    skipped = 0
    yielded = 0
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
        out = _normalize_row(rec, by_domain, store=store, clean=clean)
        if out is None:
            continue
        if skipped < offset:
            skipped += 1
            continue
        yield out
        yielded += 1
        if limit and yielded >= limit:
            break


def fetch_leads(store, **kwargs: Any) -> list[dict[str, Any]]:
    return list(iter_leads(store, **kwargs))


def count_leads(store, **kwargs: Any) -> int:
    """Count matching leads (applies clean filter in Python when needed)."""
    clean = bool(kwargs.pop("clean", False))
    # Reuse iter without limit for accurate clean counts on modest ICP sets.
    kwargs.pop("limit", None)
    kwargs.pop("offset", None)
    n = 0
    for _ in iter_leads(store, clean=clean, **kwargs):
        n += 1
    return n


def sample_leads(
    store,
    *,
    limit: int = 20,
    icp_only: bool = False,
    with_email: bool = False,
    city: str = "",
    order: Literal["random", "recent"] = "random",
    client_tag: str = "",
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
        client_tag=client_tag,
    )
    return [{k: r.get(k, "") for k in SAMPLE_COLUMNS} for r in rows]


def _client_columns(include_reason: bool) -> list[str]:
    cols = list(CLIENT_COLUMNS)
    if include_reason:
        # Keep reason after confidence for readability.
        idx = cols.index("icp_confidence") + 1
        cols.insert(idx, "icp_reason")
    return cols


def rows_to_csv(rows: list[dict[str, Any]], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for rec in rows:
        w.writerow({k: rec.get(k, "") for k in columns})
    return buf.getvalue()


def export_payload(
    store,
    *,
    icp_only: bool = True,
    with_owner: bool = False,
    with_phone: bool = False,
    with_website: bool = False,
    with_email: bool = True,
    min_rating: float = 0.0,
    min_reviews: int = 0,
    min_permits: int = 0,
    states: list[str] | None = None,
    city: str | None = None,
    state: str | None = None,
    q: str | None = None,
    source: str | None = None,
    center: str | None = None,
    radius_miles: float | None = None,
    include_reason: bool = False,
    clean: bool = True,
    cap: int = EXPORT_CAP,
    out_path: str | Path | None = None,
    backfill_cities: bool = True,
    client_tag: str = "",
) -> dict[str, Any]:
    """Build the MCP export response: CSV text + totals (capped)."""
    cities_backfilled = backfill_blank_cities(store) if backfill_cities else 0
    filter_kwargs = dict(
        icp_only=icp_only,
        with_owner=with_owner,
        with_phone=with_phone,
        with_website=with_website,
        with_email=with_email,
        min_rating=min_rating,
        min_reviews=min_reviews,
        min_permits=min_permits,
        states=states,
        city=city,
        state=state,
        q=q,
        source=source,
        center=center,
        radius_miles=radius_miles,
        order="name",
        clean=clean,
        client_tag=client_tag,
    )
    # Collect up to cap+1 to know if truncated, and count total cheaply for ICP-sized sets.
    rows: list[dict[str, Any]] = []
    total = 0
    for rec in iter_leads(store, **filter_kwargs):
        total += 1
        if len(rows) < cap:
            rows.append(rec)

    columns = _client_columns(include_reason)
    csv_text = rows_to_csv(rows, columns)

    written = None
    if out_path:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Full local write uses the richer COLUMNS set (reason always included on disk).
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
            w.writeheader()
            for rec in iter_leads(store, **filter_kwargs):
                w.writerow(rec)
        written = str(path)

    return {
        "total_matching": total,
        "capped_at": int(cap),
        "returned_rows": len(rows),
        "csv": csv_text,
        "columns": columns,
        "include_reason": include_reason,
        "clean": clean,
        "cities_backfilled": cities_backfilled,
        "out_path": written,
        "client_tag": normalize_client_tag(client_tag) or None,
    }


def query_leads(
    store,
    *,
    q: str = "",
    city: str = "",
    state: str = "",
    icp_only: bool = False,
    with_email: bool = False,
    with_owner: bool = False,
    min_permits: int = 0,
    page: int = 1,
    page_size: int = 50,
    clean: bool = True,
    include_reason: bool = False,
    backfill_cities: bool = True,
    client_tag: str = "",
) -> dict[str, Any]:
    cities_backfilled = backfill_blank_cities(store) if backfill_cities else 0
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 50), QUERY_PAGE_MAX))
    filter_kwargs = dict(
        icp_only=icp_only,
        with_email=with_email,
        with_owner=with_owner,
        min_permits=min_permits,
        city=city or None,
        state=state or None,
        q=q or None,
        order="name",
        clean=clean,
        client_tag=client_tag,
    )
    total = 0
    items_raw: list[dict[str, Any]] = []
    start = (page - 1) * page_size
    end = start + page_size
    for rec in iter_leads(store, **filter_kwargs):
        if start <= total < end:
            items_raw.append(rec)
        total += 1
    cols = _client_columns(include_reason)
    items = [{k: r.get(k, "") for k in cols} for r in items_raw]
    total_pages = max(1, math.ceil(total / page_size)) if total else 0
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "items": items,
        "clean": clean,
        "cities_backfilled": cities_backfilled,
        "client_tag": normalize_client_tag(client_tag) or None,
    }


def leads_summary(
    store,
    *,
    icp_only: bool = False,
    source: str = "",
    clean: bool = True,
    backfill_cities: bool = True,
    client_tag: str = "",
) -> dict[str, Any]:
    """Aggregate counts only — never rows.

    With client_tag, classified / in_icp / breakdowns are that client's
    business_icp rows. Without one, the payload is labelled cross-client
    and does not pretend a single in_icp flag is universal.
    """
    cities_backfilled = backfill_blank_cities(store) if backfill_cities else 0
    stats = store.stats()
    tag = normalize_client_tag(client_tag)

    if not tag:
        total = store.conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        by_client = store.icp_by_client()
        for name, counts in by_client.items():
            counts["unclassified"] = max(0, total - counts["classified"])
        return {
            "scope": "cross-client",
            "warning": (
                "No client_tag — in_icp is per client and is not reported as "
                "a single flag. Pass client_tag='basco' or "
                "client_tag='peterson' for that client's counts."
            ),
            "total_businesses": total,
            "by_client": by_client,
            "businesses_by_source": stats.get("businesses_by_source") or {},
            "clean": clean,
            "cities_backfilled": cities_backfilled,
        }

    # Base population optionally scoped.
    where = []
    args: list[Any] = []
    if source:
        where.append("COALESCE(NULLIF(b.source,''), 'maps') = ?")
        args.append(source.strip().lower())
    if icp_only:
        where.append(
            "EXISTS (SELECT 1 FROM business_icp v "
            "WHERE v.place_id=b.place_id AND v.client_tag=? AND v.in_icp=1)"
        )
        args.append(tag)
    wh = (" WHERE " + " AND ".join(where)) if where else ""

    total = store.conn.execute(
        f"SELECT COUNT(*) FROM businesses b{wh}", args
    ).fetchone()[0]
    icp_wh = wh + (" AND " if wh else " WHERE ") + "v.client_tag=?"
    icp_args = args + [tag]
    in_icp = store.conn.execute(
        f"""SELECT COUNT(*) FROM businesses b
            JOIN business_icp v ON v.place_id=b.place_id AND v.in_icp=1
            {icp_wh}""",
        icp_args,
    ).fetchone()[0]
    classified = store.conn.execute(
        f"""SELECT COUNT(*) FROM businesses b
            JOIN business_icp v ON v.place_id=b.place_id
            {icp_wh}""",
        icp_args,
    ).fetchone()[0]
    with_phone = store.conn.execute(
        f"SELECT COUNT(*) FROM businesses b{wh}"
        + (" AND " if wh else " WHERE ")
        + "b.phone IS NOT NULL AND b.phone != ''",
        args,
    ).fetchone()[0]
    with_website = store.conn.execute(
        f"SELECT COUNT(*) FROM businesses b{wh}"
        + (" AND " if wh else " WHERE ")
        + "b.domain IS NOT NULL AND b.domain != ''",
        args,
    ).fetchone()[0]
    unique_domains = store.conn.execute(
        f"SELECT COUNT(DISTINCT b.domain) FROM businesses b{wh}"
        + (" AND " if wh else " WHERE ")
        + "b.domain IS NOT NULL AND b.domain != ''",
        args,
    ).fetchone()[0]

    # with_email: count businesses that have at least one clean email when clean=True.
    with_email = 0
    by_city: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_domain = store.emails_by_domain()
    icp_sql = f"""
        SELECT b.place_id, b.domain, b.city, b.main_category
        FROM businesses b
        JOIN business_icp v ON v.place_id=b.place_id AND v.in_icp=1
        {icp_wh}
    """
    # For with_email across all (not just ICP), scan matching businesses.
    email_scan_sql = f"SELECT b.place_id, b.domain FROM businesses b{wh}"
    for row in store.conn.execute(email_scan_sql, args):
        addrs = _emails_for_row(store, dict(row), by_domain)
        if not addrs:
            continue
        ranked = email_lib.rank(addrs, row["domain"] or "", "")
        primary = ranked[0] if ranked else ""
        if not primary:
            continue
        if clean and not email_lib.is_clean_lead_email(primary, row["domain"] or ""):
            continue
        with_email += 1

    for row in store.conn.execute(icp_sql, icp_args):
        city = (row["city"] or "").strip() or "(blank)"
        cat = (row["main_category"] or "").strip() or "(blank)"
        by_city[city] = by_city.get(city, 0) + 1
        by_category[cat] = by_category.get(cat, 0) + 1

    top_cities = dict(sorted(by_city.items(), key=lambda kv: (-kv[1], kv[0]))[:25])
    top_categories = dict(
        sorted(by_category.items(), key=lambda kv: (-kv[1], kv[0]))[:25]
    )

    return {
        "scope": "client",
        "client_tag": tag,
        "total_businesses": total,
        "in_icp": in_icp,
        "with_email": with_email,
        "with_phone": with_phone,
        "with_website": with_website,
        "unique_domains": unique_domains,
        "classified": classified,
        "unclassified": max(0, total - classified),
        "in_icp_by_city": top_cities,
        "in_icp_by_main_category": top_categories,
        "businesses_by_source": stats.get("businesses_by_source") or {},
        "clean": clean,
        "cities_backfilled": cities_backfilled,
    }


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
    client_tag: str = "",
) -> int:
    """Legacy file-only export used by CLI / run_leads."""
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
            clean=False,
            client_tag=client_tag,
        ):
            w.writerow(rec)
            n += 1
    return n
