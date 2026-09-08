"""Extract US state codes from messy mailing addresses.

Handles the formats that show up in permit_parcel mailings / operators:

- ``, TX 75201`` / ``, TX, 75201``  (comma before state)
- ``BOSTON MA, 02109`` / ``DALLAS TX, 75201``  (comma between state and zip)
- ``DALLAS TX 75201``  (no comma)
- ``BOSTON MASSACHUSETTS 02116``  (full state name)

Street suffixes like ``7TH ST`` must not win over a real state near the ZIP.
"""

from __future__ import annotations

import re

US_STATE_NAMES: dict[str, str] = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
    "DISTRICT OF COLUMBIA": "DC",
}

US_STATE_CODES = set(US_STATE_NAMES.values()) | {"DC"}

# Street-type tokens that collide with real postal codes (ST=SD, CT=CT, etc.)
_STREET_SUFFIX_CODES = frozenset(
    {"ST", "DR", "LN", "CT", "AVE", "RD", "WAY", "PL", "CIR"}
)

# Ordered: most specific / end-anchored first.
_CODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ", TX, 75201" or ", TX 75201" at end (comma before state)
    re.compile(
        r",\s*([A-Za-z]{2})\s*,?\s*(\d{5})(?:-\d{4})?\s*$",
        re.I,
    ),
    # "MA, 02109" / "TX, 75201" at end (comma between state and zip)
    re.compile(
        r"\b([A-Za-z]{2})\s*,\s*(\d{5})(?:-\d{4})?\s*$",
        re.I,
    ),
    # "TX 75201" at end
    re.compile(
        r"\b([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?\s*$",
        re.I,
    ),
)


def _looks_like_street_suffix(text: str, match_start: int, code: str) -> bool:
    """Skip ``123 MAIN ST, 02109`` style false positives for street suffixes."""
    if code not in _STREET_SUFFIX_CODES:
        return False
    window = text[max(0, match_start - 48) : match_start]
    return bool(re.search(r"\b\d{1,6}\s+\S", window))

# Full names, longest first so "NEW YORK" beats "YORK".
_NAME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        code,
        re.compile(
            rf"\b{re.escape(name)}\b\s*,?\s*(\d{{5}})(?:-\d{{4}})?\s*$",
            re.I,
        ),
    )
    for name, code in sorted(US_STATE_NAMES.items(), key=lambda kv: -len(kv[0]))
)


def extract_state(address: str) -> str:
    """Return 2-letter USPS state code, or '' if none can be parsed."""
    text = (address or "").strip()
    if not text:
        return ""
    upper = text.upper()

    for pat in _CODE_PATTERNS:
        m = pat.search(upper)
        if not m:
            continue
        code = m.group(1).upper()
        if code not in US_STATE_CODES:
            continue
        if _looks_like_street_suffix(upper, m.start(1), code):
            continue
        return code

    for code, pat in _NAME_PATTERNS:
        if pat.search(upper):
            return code

    return ""


def is_in_states(address: str, allowed: list[str] | tuple[str, ...] | set[str]) -> bool:
    """True when address parses to one of the allowed state codes."""
    allowed_set = {s.strip().upper() for s in allowed if s and str(s).strip()}
    if not allowed_set:
        return True
    st = extract_state(address)
    if not st:
        return False
    return st in allowed_set
