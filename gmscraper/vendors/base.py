"""Shared types for enrichment vendor clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PersonHit:
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    title: str = ""
    job_level: str = ""
    email: str = ""
    linkedin_url: str = ""
    phone: str = ""
    source_tier: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        if self.full_name:
            return self.full_name
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class EmailHit:
    email: str
    source_tier: str
    status: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def split_name(full: str) -> tuple[str, str]:
    """Split a full name into (first, last), dropping generational suffixes.

    ``Leo Karl III`` → ``("Leo", "Karl")`` — not last=``III``.
    """
    parts = [p for p in (full or "").replace(",", " ").split() if p]
    while len(parts) >= 2 and _is_name_suffix(parts[-1]):
        parts = parts[:-1]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


_NAME_SUFFIXES = {
    "jr", "sr", "esq", "md", "phd", "phd.", "cpa", "dds", "do", "dvm",
}
_NAME_SUFFIX_ROMAN = {"ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def _is_name_suffix(token: str) -> bool:
    t = (token or "").strip().rstrip(".").lower()
    if not t:
        return False
    if t in _NAME_SUFFIXES or t in _NAME_SUFFIX_ROMAN:
        return True
    return False
