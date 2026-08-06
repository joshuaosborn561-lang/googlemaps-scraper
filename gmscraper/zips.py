"""US ZIP code list.

Built offline from the `zipcodes` package, which ships the USPS-derived table
inside the wheel -- no download, no API key, and the file is reproducible on
any machine that installs requirements.txt.

ZIP types, and why the default is what it is:

    STANDARD  30,006  ordinary geographic ZIPs -- where businesses actually are
    PO BOX     9,412  a wall of PO boxes inside some other ZIP's footprint
    UNIQUE     2,548  a single high-volume address (a university, a big HQ)
    MILITARY     823  APO/FPO/DPO

A Maps search is a *geographic* radius search around the ZIP's centroid, so
PO BOX and MILITARY ZIPs return the same businesses their surrounding
STANDARD ZIP already returned -- you pay for the request and dedup throws the
rows away.  `--types STANDARD` (the default) is the efficient list.  Use
`--types all` if you would rather pay for the overlap.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Sequence

FIELDS = ["zip", "city", "state", "county", "lat", "lng", "type"]
ALL_TYPES = ("STANDARD", "PO BOX", "UNIQUE", "MILITARY")

# The 50 states + DC.  Territories (PR, VI, GU, AS, MP, FM, MH, PW) and the
# military pseudo-states (AA/AE/AP) are excluded by default.
STATES_50 = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
}


def build(
    out_path: str | Path,
    types: Sequence[str] = ("STANDARD",),
    include_territories: bool = False,
    active_only: bool = True,
) -> int:
    """Write the ZIP CSV and return the row count."""
    try:
        import zipcodes
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit("pip install -r requirements.txt (missing `zipcodes`)") from exc

    wanted = {t.upper() for t in types}
    if "ALL" in wanted:
        wanted = set(ALL_TYPES)

    rows = []
    for z in zipcodes.list_all():
        if active_only and not z.get("active", True):
            continue
        if z.get("zip_code_type", "").upper() not in wanted:
            continue
        state = (z.get("state") or "").upper()
        if not include_territories and state not in STATES_50:
            continue
        if not z.get("lat") or not z.get("long"):
            continue
        rows.append(
            {
                "zip": z["zip_code"],
                "city": z.get("city", ""),
                "state": state,
                "county": z.get("county", ""),
                "lat": z["lat"],
                "lng": z["long"],
                "type": z.get("zip_code_type", ""),
            }
        )

    rows.sort(key=lambda r: r["zip"])
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def load(
    path: str | Path,
    states: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, str]]:
    """Read the ZIP CSV, optionally filtered to a set of states."""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"{p} not found -- run `python -m gmscraper zips` first.")
    keep = {s.upper() for s in states} if states else None
    rows = []
    with p.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if keep and row["state"].upper() not in keep:
                continue
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    return rows
