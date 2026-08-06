"""Pending spend approvals for paid MCP tools.

Flow:
  1. plan_leads / estimate_cost creates an approval record + token
  2. Claude shows the cost to the user
  3. User says yes in chat
  4. Claude calls a paid tool with i_approve_spend=True and that approval_id
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPROVALS_DIR = ROOT / "data" / "approvals"
# Matches CLAUDE.md: auto-approve under $5 overage when caller opts in.
AUTO_APPROVE_UNDER_USD = 5.0
APPROVAL_TTL_SECONDS = 60 * 60 * 6  # 6 hours


@dataclass
class Approval:
    id: str
    brief: str
    plan_path: str
    requests: int
    estimated_overage_usd: float | None  # None = unknown; inf encoded as -1 blocked
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
            "auto_approve_under_usd": AUTO_APPROVE_UNDER_USD,
            "expires_at": self.expires_at,
            "instruction": (
                "Show this estimate to the user. When they say yes, call "
                "run_leads (or scrape_maps) with approval_id="
                f"{self.id} and i_approve_spend=true."
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
    i_approve_spend: bool,
    allow_auto_under: bool = True,
) -> Approval:
    """Gate paid actions. Claude must pass i_approve_spend=true after the user says yes."""
    approval = load_approval(approval_id)
    if approval.expired:
        raise ValueError("This approval expired. Re-run plan_leads to get a fresh estimate.")
    if approval.used:
        raise ValueError(
            "This approval was already used. Re-run plan_leads if you want another paid run."
        )
    if approval.blocked:
        raise ValueError(
            "This run is BLOCKED on the current Maps plan (hard limit). "
            "Upgrade MAPS_PLAN before approving."
        )

    overage = approval.estimated_overage_usd or 0.0
    under_auto = allow_auto_under and overage <= AUTO_APPROVE_UNDER_USD

    if not i_approve_spend and not under_auto:
        raise ValueError(
            "Spend not approved. Show the estimate to the user; when they say yes, "
            f"retry with i_approve_spend=true and approval_id={approval_id}."
        )

    if not i_approve_spend and under_auto:
        # Still require explicit Claude flag for MCP clarity — user said keep gates
        # but allow saying yes in Claude. Under $5 we still want i_approve_spend
        # unless they set GMAPS_MCP_AUTO_APPROVE=1.
        import os

        if os.environ.get("GMAPS_MCP_AUTO_APPROVE", "").strip() not in ("1", "true", "yes"):
            raise ValueError(
                f"Estimated overage ${overage:.2f} is under ${AUTO_APPROVE_UNDER_USD:.2f}, "
                "but MCP still needs i_approve_spend=true after the user says yes "
                "(or set GMAPS_MCP_AUTO_APPROVE=1 to skip that for sub-$5 runs)."
            )

    return approval
