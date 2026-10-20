"""Territory lock for Maps results.

A Maps search seeded from a ZIP can return listings in other states (a
national-brand result, a bad geocode). Nothing downstream re-checked the
Maps state, so those listings were stored and later loaded. This module
drops them at scrape time.
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any, Iterable, Sequence

STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY", "PUERTO RICO": "PR",
}
CODES = set(STATE_NAMES.values())


def norm_state(value: Any) -> str:
    s = str(value or "").strip().upper().replace(".", "")
    if s in CODES:
        return s
    return STATE_NAMES.get(s, "")


def allowed_state_set(
    allowed_states: Sequence[str] | None, zip_rows: Iterable[dict[str, Any]]
) -> frozenset[str]:
    """Explicit states win; otherwise the states of the seed ZIPs."""
    if allowed_states:
        out = {norm_state(s) for s in allowed_states}
    else:
        out = {norm_state(z.get("state")) for z in zip_rows}
    out.discard("")
    return frozenset(out)


class DropCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.by_state: Counter[str] = Counter()

    def add(self, state: str) -> None:
        with self._lock:
            self.by_state[state] += 1

    @property
    def total(self) -> int:
        return sum(self.by_state.values())


def filter_rows(
    rows: list[dict[str, Any]], allowed: frozenset[str], drops: DropCounter
) -> list[dict[str, Any]]:
    """Keep rows inside ``allowed``. Rows with no state are kept (the pre-load
    gate holds them); an empty ``allowed`` set disables the filter."""
    if not allowed:
        return rows
    kept = []
    for r in rows:
        st = norm_state(r.get("state"))
        if st and st not in allowed:
            drops.add(st)
            continue
        kept.append(r)
    return kept
