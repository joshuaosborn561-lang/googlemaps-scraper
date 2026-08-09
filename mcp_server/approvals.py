"""Saved scrape plans for paid MCP tools.

`plan_leads` / `estimate_cost` write a plan record. Paid tools can load it by
`approval_id` (kept for compatibility) OR by `plan_path`, OR fall back to the
latest saved plan. There is no spend-approval gate and no i_approve_spend check
— Claude Web does not surface approval ids reliably, so tools must run without them.
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
APPROVAL_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days (informational only)


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
                f"Call run_leads with plan_path={self.plan_path!r} "
                f"(or approval_id={self.id}). No approval / auth required."
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
            f"Unknown approval_id {approval_id!r}. "
            "Pass plan_path from plan_leads, or omit both to use the latest plan."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return Approval(**data)


def latest_approval() -> Approval | None:
    """Most recently created plan record, if any."""
    APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(APPROVALS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Approval(**data)
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    return None


def resolve_plan(
    *,
    approval_id: str = "",
    plan_path: str = "",
) -> Approval:
    """Resolve a plan without any spend-approval gate.

    Precedence: explicit approval_id → plan_path → latest saved plan.
    Expired / already-used plans are still accepted (Claude often reconnects).
    Only hard-blocked plan estimates (Maps hard limit) are refused.
    """
    approval: Approval | None = None
    if (approval_id or "").strip():
        approval = load_approval(approval_id.strip())
    elif (plan_path or "").strip():
        path = Path(plan_path.strip())
        if not path.exists():
            raise ValueError(f"plan_path not found: {plan_path!r}")
        # Synthetic approval wrapping a direct plan file.
        approval = Approval(
            id="plan_path",
            brief="",
            plan_path=str(path),
            requests=0,
            estimated_overage_usd=0.0,
            blocked=False,
            states=[],
            categories=[],
            vertical="",
            created_at=time.time(),
            expires_at=time.time() + APPROVAL_TTL_SECONDS,
            used=False,
        )
    else:
        approval = latest_approval()
        if approval is None:
            raise ValueError(
                "No plan found. Call plan_leads (or estimate_cost) first, "
                "then run_leads — approval_id is optional."
            )

    if approval.blocked:
        raise ValueError(
            "This run is BLOCKED on the current Maps plan (hard limit). "
            "Upgrade MAPS_PLAN before running."
        )
    if not approval.plan_path or not Path(approval.plan_path).exists():
        raise ValueError(
            f"Plan file missing at {approval.plan_path!r}. Re-run plan_leads."
        )
    return approval


def mark_used(approval_id: str) -> None:
    """Best-effort mark; never required for subsequent runs."""
    if not (approval_id or "").strip() or approval_id == "plan_path":
        return
    try:
        approval = load_approval(approval_id)
    except ValueError:
        return
    approval.used = True
    _path(approval_id).write_text(json.dumps(asdict(approval), indent=2), encoding="utf-8")


def require_spend_approval(
    *,
    approval_id: str = "",
    plan_path: str = "",
    i_approve_spend: bool = True,
    allow_auto_under: bool = True,
) -> Approval:
    """Backward-compatible alias — no spend gate, just resolve the plan."""
    del i_approve_spend, allow_auto_under
    return resolve_plan(approval_id=approval_id, plan_path=plan_path)
