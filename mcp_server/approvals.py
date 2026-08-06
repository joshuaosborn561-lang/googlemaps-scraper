"""Saved scrape plans for paid MCP tools.

`plan_leads` / `estimate_cost` write a plan record. Paid tools load it by
`approval_id` (kept as the field name for compatibility). No login auth and
no i_approve_spend flag — the connector is open.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPROVALS_DIR = ROOT / "data" / "approvals"
AUTO_APPROVE_UNDER_USD = 5.0
APPROVAL_TTL_SECONDS = 60 * 60 * 6  # 6 hours


@dataclass
class Approval:
    id: str
    brief: str
    plan_path: str
    requests: int
    estimated_overage_usd: float | None
    blocked: bool
    states: list[str]
    categories: list[str]
    vertical: str
    created_at: float
    expires_at: float
    used: bool = False

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    def to_public(self) -> dict:
        return {
            "approval_id": self.id,
            "brief": self.brief,
            "plan_path": self.plan_path,
            "vertical": self.vertical,
            "states": self.states or ["US (nationwide)"],
            "categories": self.categories,
            "requests": self.requests,
            "estimated_overage_usd": (
                "BLOCKED" if self.blocked else self.estimated_overage_usd
            ),
            "expires_at": self.expires_at,
            "instruction": (
                f"Call run_leads (or scrape_maps) with approval_id={self.id}. "
                "No auth / i_approve_spend flag required."
            ),
        }


def _path(approval_id: str) -> Path:
    APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
    return APPROVALS_DIR / f"{approval_id}.json"


def create_approval(
    *,
    brief: str,
    plan_path: str,
    requests: int,
    estimated_overage_usd: float | None,
    blocked: bool,
    states: list[str],
    categories: list[str],
    vertical: str,
) -> Approval:
    approval = Approval(
        id=secrets.token_urlsafe(12),
        brief=brief,
        plan_path=plan_path,
        requests=requests,
        estimated_overage_usd=estimated_overage_usd,
        blocked=blocked,
        states=list(states),
        categories=list(categories),
        vertical=vertical,
        created_at=time.time(),
        expires_at=time.time() + APPROVAL_TTL_SECONDS,
    )
    _path(approval.id).write_text(json.dumps(asdict(approval), indent=2), encoding="utf-8")
    return approval


def load_approval(approval_id: str) -> Approval:
    path = _path(approval_id)
    if not path.exists():
        raise ValueError(
            f"Unknown approval_id {approval_id!r}. Call plan_leads or estimate_cost first."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return Approval(**data)


def mark_used(approval_id: str) -> None:
    approval = load_approval(approval_id)
    approval.used = True
    _path(approval_id).write_text(json.dumps(asdict(approval), indent=2), encoding="utf-8")


def require_spend_approval(
    *,
    approval_id: str,
    i_approve_spend: bool = True,
    allow_auto_under: bool = True,
) -> Approval:
    """Load a saved plan. No auth / spend-flag check — only block hard failures."""
    del i_approve_spend, allow_auto_under  # accepted for backward-compatible callers
    approval = load_approval(approval_id)
    if approval.expired:
        raise ValueError("This plan expired. Re-run plan_leads to get a fresh estimate.")
    if approval.used:
        raise ValueError(
            "This plan was already used. Re-run plan_leads if you want another paid run."
        )
    if approval.blocked:
        raise ValueError(
            "This run is BLOCKED on the current Maps plan (hard limit). "
            "Upgrade MAPS_PLAN before running."
        )
    return approval
