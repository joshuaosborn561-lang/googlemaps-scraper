"""Final stage: CSV out."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from . import emails as email_lib

COLUMNS = [
    "place_id", "name", "owner_name", "owner_title", "owner_source",
    "email", "all_emails", "phone", "website", "domain",
    "address", "city", "state", "zip",
    "rating", "reviews", "main_category", "types", "latitude", "longitude",
    "maps_url", "in_icp", "icp_confidence", "icp_reason", "source_category",
]

BASE_SQL = """
SELECT b.place_id, b.name, b.phone, b.website, b.domain, b.address, b.city,
       b.state, b.zip, b.rating, b.reviews, b.main_category, b.types,
       b.latitude, b.longitude, b.maps_url, b.source_category,
       v.in_icp, v.confidence AS icp_confidence, v.reason AS icp_reason,
       o.owner_name, o.owner_title, o.source AS owner_source
FROM businesses b
LEFT JOIN verdicts v ON v.place_id = b.place_id
LEFT JOIN owners   o ON o.place_id = b.place_id
"""


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
) -> int:
    clauses, args = [], []
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

    sql = BASE_SQL + (" WHERE " + " AND ".join(clauses) if clauses else "")
    sql += " ORDER BY b.state, b.city, b.name"

    # Emails are ranked per row, because the best address depends on who the
    # owner turned out to be (margaret@ beats info@ for a business Margaret owns).
    by_domain = store.emails_by_domain()

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in store.conn.execute(sql, args):
            rec = dict(row)
            try:
                rec["types"] = ", ".join(json.loads(rec.get("types") or "[]"))
            except (json.JSONDecodeError, TypeError):
                rec["types"] = rec.get("types") or ""
            if rec.get("in_icp") is not None:
                rec["in_icp"] = "yes" if rec["in_icp"] else "no"

            ranked = email_lib.rank(
                by_domain.get(rec.get("domain") or "", []),
                rec.get("domain") or "",
                rec.get("owner_name") or "",
            )
            rec["email"] = ranked[0] if ranked else ""
            rec["all_emails"] = ", ".join(ranked[1:6])

            w.writerow(rec)
            n += 1
    return n
