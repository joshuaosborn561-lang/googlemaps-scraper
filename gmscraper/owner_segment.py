"""Classify operator / mailing entities into buyer-motion segments.

Segments are about *what kind of organisation* it is (how you sell to them),
not public-vs-private funding. A private university is ``education``; a city
is ``municipal``. Ambiguous rows stay ``unclassified`` rather than guessed.
"""

from __future__ import annotations

import re
from typing import Any

# Stable vocabulary — used as filter params and stored on operator rows.
SEGMENTS: tuple[str, ...] = (
    "municipal",
    "education",
    "healthcare",
    "housing_authority",
    "utility_transit",
    "religious_nonprofit",
    "private",
    "unclassified",
)

DEFAULT_RESOLVE_SEGMENTS = ("private",)

_SEGMENT_SET = set(SEGMENTS)


def parse_segments(raw: str | list[str] | None, *, default: tuple[str, ...] | None = None) -> list[str]:
    """Parse comma-separated segments; ``all`` / ``*`` means every segment."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return list(default or DEFAULT_RESOLVE_SEGMENTS)
    if isinstance(raw, list):
        parts = [str(x).strip().lower() for x in raw if str(x).strip()]
    else:
        text = str(raw).strip().lower()
        if text in ("all", "*", "any", "everything"):
            return list(SEGMENTS)
        parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return list(default or DEFAULT_RESOLVE_SEGMENTS)
    if any(p in ("all", "*", "any", "everything") for p in parts):
        return list(SEGMENTS)
    bad = [p for p in parts if p not in _SEGMENT_SET]
    if bad:
        raise ValueError(
            f"Unknown owner_segment(s) {bad}. Allowed: {', '.join(SEGMENTS)} "
            "or 'all'."
        )
    # Preserve order, unique
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _blob(*parts: str) -> str:
    return " ".join(" ".join(str(p or "").upper().split()) for p in parts if p)


# Ordered: first match wins. More specific institutional patterns before private.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "housing_authority",
        re.compile(
            r"\b("
            r"HOUSING\s+AUTHORITY|HOUSING\s+FINANCE(\s+CORP|\s+CORPORATION|)|"
            r"HFC\b|HA\b(?=.*HOUSING)|"
            r"PUBLIC\s+HOUSING"
            r")\b"
        ),
    ),
    (
        "education",
        re.compile(
            r"\b("
            r"INDEPENDENT\s+SCHOOL\s+DISTRICT|\bISD\b|SCHOOL\s+DISTRICT|"
            r"BOARD\s+OF\s+REGENTS|BOARD\s+OF\s+REG\b|"
            r"UNIVERSITY(\s+SYSTEM)?|COLLEGE|COMMUNITY\s+COLLEGE|"
            r"SCHOOLS?\b|ACADEMY|EDUCATION\s+SERVICE\s+CENTER|\bESC\b|"
            r"TEXAS\s+A\s*&\s*M|UT\s+SYSTEM|A&M\s+SYSTEM"
            r")\b"
        ),
    ),
    (
        "healthcare",
        re.compile(
            r"\b("
            r"HOSPITAL(\s+DISTRICT|\s+SYSTEM|\s+AUTHORITY)?|"
            r"MEDICAL\s+CENTER|HEALTH\s+SYSTEM|HEALTHCARE|"
            r"HEALTH\s+&\s+HOSP|HEALTH\s+AND\s+HOSP|"
            r"CLINIC\b|PHYSICIANS?\b|METHODIST\s+HOSPITALS|"
            r"PARKLAND|CHILDRENS?\s+MEDICAL|BAYLOR\s+SCOTT"
            r")\b"
        ),
    ),
    (
        "utility_transit",
        re.compile(
            r"\b("
            r"TRANSIT(\s+AUTHORITY)?|\bDART\b|MTA\b|METRO\b|"
            r"WATER\s+(DISTRICT|AUTHORITY|CONTROL)|"
            r"\bMUD\b|MUNICIPAL\s+UTILITY|UTILITY\s+DISTRICT|"
            r"ELECTRIC\s+(COOP|COOPERATIVE|UTILITY)|"
            r"APPRAISAL\s+DISTRICT|FLOOD\s+CONTROL|"
            r"TURNPIKE|TOLLWAY|PORT\s+AUTHORITY|AIRPORT\s+AUTHORITY"
            r")\b"
        ),
    ),
    (
        "municipal",
        re.compile(
            r"\b("
            r"CITY\s+OF|TOWN\s+OF|VILLAGE\s+OF|COUNTY\s+OF|"
            r"CITY\s+OF\s+\w+|COUNTY\s+OF\s+\w+|"
            r"\w+\s+CITY\s+OF|\w+\s+COUNTY\s+OF|"
            r"STATE\s+OF|UNITED\s+STATES|\bUSA\b|\bUS\s+GOV|"
            r"MUNICIPAL(\s+AUTHORITY)?|COUNTY\s+\w+\s+OF|"
            r"\bTXDOT\b|DEPARTMENT\s+OF\s+TRANSPORTATION|"
            r"GENERAL\s+LAND\s+OFFICE|\bGLO\b|"
            r"FIRE\s+DEPARTMENT|POLICE\s+DEPARTMENT|"
            r"PUBLIC\s+WORKS|PARKS?\s+AND\s+REC"
            r")\b"
        ),
    ),
    (
        "religious_nonprofit",
        re.compile(
            r"\b("
            r"CHURCH|DIOCESE|PARISH|TEMPLE|MOSQUE|SYNAGOGUE|"
            r"MINISTRIES|CATHOLIC|BAPTIST|METHODIST\s+CHURCH|"
            r"PRESBYTERIAN|LUTHERAN|EPISCOPAL|"
            r"YMCA|YWCA|SALVATION\s+ARMY|"
            r"CHARITABLE\s+TRUST|RELIGIOUS\s+"
            r")\b"
        ),
    ),
)

# Strong private signals — only used to avoid unclassified when no institutional hit.
_PRIVATE_HINT = re.compile(
    r"\b("
    r"LLC|L\.?L\.?C\.?|LP\b|L\.?P\.?|LTD|LIMITED\s+PARTNERSHIP|"
    r"INC\b|INCORPORATED|CORP\b|CORPORATION|"
    r"PROPERTIES|INVESTMENTS?|HOLDINGS|PARTNERS|REALTY|CAPITAL|"
    r"INDUSTRIAL|DEVELOPMENT|MANAGEMENT|VENTURES|TRUST\s+U/?T/?A"
    r")\b"
)

# Weak / ambiguous — prefer unclassified over a wrong institutional bucket.
_AMBIGUOUS = re.compile(
    r"\b("
    r"FOUNDATION|ENDOWMENT|ASSOCIATION|AUTHORITY\b|"
    r"TRUST\b|TRUSTEE|ESTATE\s+OF|C/O\b|CARE\s+OF"
    r")\b"
)


def classify_owner_segment(
    *,
    top_llc: str = "",
    operator_address: str = "",
    owner_name: str = "",
) -> str:
    """Return one of SEGMENTS for an operator / mailing entity."""
    text = _blob(top_llc, owner_name, operator_address)
    if not text.strip():
        return "unclassified"

    for segment, pat in _RULES:
        if pat.search(text):
            return segment

    if _AMBIGUOUS.search(text) and not _PRIVATE_HINT.search(text):
        return "unclassified"

    if _PRIVATE_HINT.search(text):
        return "private"

    # Bare person-looking or unknown org names: private is the useful default
    # for outreach ranking, but keep truly empty/noise as unclassified.
    if re.search(r"[A-Z]{2,}", text):
        return "private"
    return "unclassified"


def classify_operator_row(row: dict[str, Any]) -> str:
    return classify_owner_segment(
        top_llc=str(row.get("top_llc") or ""),
        operator_address=str(row.get("operator_address") or ""),
        owner_name=str(row.get("owner_name") or ""),
    )


def segment_breakdown(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Counts + portfolio value per segment (for dry-run QA)."""
    out: dict[str, dict[str, Any]] = {
        s: {"operators": 0, "parcels": 0, "portfolio_value": 0} for s in SEGMENTS
    }
    for r in rows:
        seg = str(r.get("owner_segment") or classify_operator_row(r))
        if seg not in out:
            seg = "unclassified"
        out[seg]["operators"] += 1
        try:
            out[seg]["parcels"] += int(float(r.get("parcels") or 0))
        except (TypeError, ValueError):
            pass
        try:
            out[seg]["portfolio_value"] += int(float(r.get("portfolio_value") or 0))
        except (TypeError, ValueError):
            pass
    # Drop empty buckets for a tighter dry-run payload, keep private always.
    return {
        k: v
        for k, v in out.items()
        if v["operators"] > 0 or k == "private"
    }


def sql_in_list(segments: list[str]) -> str:
    """Safe SQL fragment for owner_segment IN (...). Segments already validated."""
    parts = []
    for s in segments:
        if s not in _SEGMENT_SET:
            raise ValueError(f"invalid segment {s!r}")
        parts.append("'" + s.replace("'", "''") + "'")
    return "(" + ", ".join(parts) + ")"
