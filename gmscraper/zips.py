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

Radius / explicit-ZIP selection (plan_leads) may intentionally include all
types so a "within N miles of X" brief covers every USPS centroid in range.
"""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path
from typing import Sequence

FIELDS = ["zip", "city", "state", "county", "lat", "lng", "type"]
ALL_TYPES = ("STANDARD", "PO BOX", "UNIQUE", "MILITARY")
EARTH_RADIUS_MILES = 3958.7613

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


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


def _row_from_package(z: dict) -> dict[str, str]:
    return {
        "zip": z["zip_code"],
        "city": z.get("city", "") or "",
        "state": (z.get("state") or "").upper(),
        "county": z.get("county", "") or "",
        "lat": str(z.get("lat") or ""),
        "lng": str(z.get("long") or ""),
        "type": z.get("zip_code_type", "") or "",
    }


def parse_center(center: str) -> tuple[float, float, str]:
    """Resolve '32.7767,-97.0000' or 'Dallas, TX' to (lat, lng, label)."""
    text = (center or "").strip()
    if not text:
        raise ValueError("center is empty")

    coord = re.match(
        r"^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$",
        text,
    )
    if coord:
        lat, lng = float(coord.group(1)), float(coord.group(2))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError(f"center coordinates out of range: {text}")
        return lat, lng, f"{lat:.4f},{lng:.4f}"

    # "Dallas, TX" / "Dallas TX" / "Fort Worth, Texas"
    m = re.match(
        r"^\s*([A-Za-z .'-]+?)\s*,?\s*([A-Za-z]{2}|[A-Za-z]+)\s*$",
        text,
    )
    if not m:
        raise ValueError(
            f"Could not parse center {text!r}. Use 'City, ST' or 'lat,lng'."
        )
    city = m.group(1).strip()
    state_raw = m.group(2).strip().upper()
    state_aliases = {"TEXAS": "TX", "OKLAHOMA": "OK", "CALIFORNIA": "CA"}
    state = state_aliases.get(state_raw, state_raw)
    if len(state) != 2 or state not in STATES_50:
        raise ValueError(f"Unknown state in center {text!r}")

    try:
        import zipcodes
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("pip install -r requirements.txt (missing `zipcodes`)") from exc

    hits = [
        z
        for z in zipcodes.filter_by(city=city.title(), state=state)
        if z.get("lat") and z.get("long")
    ]
    if not hits:
        # try original casing / uppercase city tokens
        hits = [
            z
            for z in zipcodes.filter_by(city=city, state=state)
            if z.get("lat") and z.get("long")
        ]
    if not hits:
        raise ValueError(f"No ZIP centroids found for center {text!r}")

    lat = sum(float(z["lat"]) for z in hits) / len(hits)
    lng = sum(float(z["long"]) for z in hits) / len(hits)
    return lat, lng, f"{city.title()}, {state}"


def parse_zip_list(zips: str | Sequence[str], *, max_zips: int = 5000) -> list[str]:
    """Parse comma/whitespace-separated 5-digit ZIPs. Supports >= 1000 entries."""
    if isinstance(zips, str):
        tokens = re.split(r"[\s,;]+", zips.strip())
    else:
        tokens = list(zips)
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if not re.fullmatch(r"\d{5}", tok):
            raise ValueError(f"Invalid ZIP {tok!r} — expected 5 digits")
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) > max_zips:
            raise ValueError(f"Too many ZIPs (max {max_zips})")
    if not out:
        raise ValueError("zips list is empty")
    return out


def load_by_zips(
    path: str | Path,
    zip_codes: Sequence[str],
) -> list[dict[str, str]]:
    """Return rows for an explicit ZIP list (order preserved). Missing ZIPs synthesized."""
    wanted = parse_zip_list(zip_codes)
    by_zip = {r["zip"]: r for r in load(path)}
    # Fill gaps from the package so explicit lists still work for PO BOX etc.
    missing = [z for z in wanted if z not in by_zip]
    if missing:
        try:
            import zipcodes
        except ImportError:
            zipcodes = None  # type: ignore
        if zipcodes is not None:
            for zcode in missing:
                matches = zipcodes.matching(zcode)
                if matches:
                    by_zip[zcode] = _row_from_package(matches[0])
    rows = []
    for zcode in wanted:
        row = by_zip.get(zcode)
        if row is None:
            rows.append(
                {
                    "zip": zcode,
                    "city": "",
                    "state": "",
                    "county": "",
                    "lat": "",
                    "lng": "",
                    "type": "EXPLICIT",
                }
            )
        else:
            rows.append(row)
    return rows


def within_radius(
    lat: float,
    lng: float,
    radius_miles: float,
    *,
    states: Sequence[str] | None = None,
    include_inactive: bool = True,
    types: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    """All USPS ZIP centroids within radius_miles of (lat, lng).

    Uses the zipcodes package (not just STANDARD CSV rows) so a DFW 150mi
    query reproduces ~905 Texas ZIPs including PO BOX / UNIQUE / inactive.
    """
    if radius_miles <= 0:
        raise ValueError("radius_miles must be positive")
    try:
        import zipcodes
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("pip install -r requirements.txt (missing `zipcodes`)") from exc

    keep_states = {s.upper() for s in states} if states else None
    keep_types = {t.upper() for t in types} if types else None
    rows: list[dict[str, str]] = []
    for z in zipcodes.list_all():
        if not include_inactive and not z.get("active", True):
            continue
        state = (z.get("state") or "").upper()
        if state not in STATES_50:
            continue
        if keep_states and state not in keep_states:
            continue
        ztype = (z.get("zip_code_type") or "").upper()
        if keep_types and ztype not in keep_types:
            continue
        if not z.get("lat") or not z.get("long"):
            continue
        zlat, zlng = float(z["lat"]), float(z["long"])
        if haversine_miles(lat, lng, zlat, zlng) <= radius_miles:
            rows.append(_row_from_package(z))
    rows.sort(key=lambda r: r["zip"])
    return rows


def resolve_zip_rows(
    path: str | Path,
    *,
    zips: Sequence[str] | None = None,
    center: str | None = None,
    radius_miles: float | None = None,
    center_lat: float | None = None,
    center_lng: float | None = None,
    states: Sequence[str] | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, str]], dict]:
    """Resolve ZIP rows with precedence: explicit zips > center/radius > states.

    Returns (rows, meta) where meta records how geography was resolved.
    """
    meta: dict = {
        "geo_mode": "states",
        "center": center or "",
        "center_lat": center_lat,
        "center_lng": center_lng,
        "radius_miles": radius_miles,
        "explicit_zips": 0,
    }

    if zips:
        rows = load_by_zips(path, zips)
        meta["geo_mode"] = "explicit_zips"
        meta["explicit_zips"] = len(rows)
    elif (center or (center_lat is not None and center_lng is not None)) and radius_miles:
        if center_lat is not None and center_lng is not None:
            lat, lng = float(center_lat), float(center_lng)
            label = center or f"{lat:.4f},{lng:.4f}"
        else:
            lat, lng, label = parse_center(center or "")
        rows = within_radius(lat, lng, float(radius_miles), states=states)
        meta.update(
            {
                "geo_mode": "radius",
                "center": label,
                "center_lat": lat,
                "center_lng": lng,
                "radius_miles": float(radius_miles),
            }
        )
    else:
        rows = load(path, states=states, limit=None)
        meta["geo_mode"] = "states" if states else "nationwide"

    if limit:
        rows = rows[:limit]
    return rows, meta

