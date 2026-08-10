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
    parts = [p for p in (full or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]
