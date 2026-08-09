"""Saved scrape plans for paid MCP tools.

plan_leads / estimate_cost write a plan file + index record. Paid tools load by
plan_path or the latest saved plan. There is no spend-approval gate, no
approval_id requirement, and no i_approve_spend check.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLANS_INDEX_DIR = ROOT / "data" / "approvals"  # legacy path; keep for existing files
AUTO_APPROVE_UNDER_USD = 5.0  # reporting only; never gates a run


@dataclass
class PlanRecord:
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
    expires_at: float = 0.0
    used: bool = False

    def to_public(self) -> dict:
        return {
            "plan_id": self.id,
            "brief": self.brief,
            "plan_path": self.plan_path,
            "vertical": self.vertical,
            "states": self.states or ["US (nationwide)"],
            "categories": self.categories,
            "requests": self.requests,
            "estimated_overage_usd": (
                "BLOCKED" if self.blocked else self.estimated_overage_usd
            ),
            "instruction": (
                f"Call run_leads(plan_path={self.plan_path!r}) or run_leads() "
                "to use the latest plan. No approval required."
            ),
        }


# Backward-compatible aliases so older imports keep working.
Approval = PlanRecord


def _path(plan_id: str) -> Path:
    PLANS_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    return PLANS_INDEX_DIR / f"{plan_id}.json"


def save_plan_record(
    *,
    brief: str,
    plan_path: str,
    requests: int,
    estimated_overage_usd: float | None,
    blocked: bool,
    states: list[str],
    categories: list[str],
    vertical: str,
) -> PlanRecord:
    rec = PlanRecord(
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
    )
    _path(rec.id).write_text(json.dumps(asdict(rec), indent=2), encoding="utf-8")
    return rec


# Legacy name used across server.py — keep as thin alias.
create_approval = save_plan_record


def latest_plan() -> PlanRecord | None:
    PLANS_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        PLANS_INDEX_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Drop fields we no longer require
            data.pop("expires_at", None)
            return PlanRecord(
                id=data["id"],
                brief=data.get("brief") or "",
                plan_path=data["plan_path"],
                requests=int(data.get("requests") or 0),
                estimated_overage_usd=data.get("estimated_overage_usd"),
                blocked=bool(data.get("blocked")),
                states=list(data.get("states") or []),
                categories=list(data.get("categories") or []),
                vertical=data.get("vertical") or "",
                created_at=float(data.get("created_at") or 0),
                used=bool(data.get("used")),
            )
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    return None


latest_approval = latest_plan


def resolve_plan(
    *,
    plan_path: str = "",
    plan_id: str = "",
    approval_id: str = "",  # ignored legacy alias
) -> PlanRecord:
    """Load a saved plan. No spend gate. Never refuses for used/expired."""
    del approval_id  # never gate on legacy approval ids
    if (plan_path or "").strip():
        path = Path(plan_path.strip())
        if not path.exists():
            raise ValueError(f"plan_path not found: {plan_path!r}")
        return PlanRecord(
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
        )

    if (plan_id or "").strip():
        path = _path(plan_id.strip())
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return PlanRecord(
                id=data["id"],
                brief=data.get("brief") or "",
                plan_path=data["plan_path"],
                requests=int(data.get("requests") or 0),
                estimated_overage_usd=data.get("estimated_overage_usd"),
                blocked=bool(data.get("blocked")),
                states=list(data.get("states") or []),
                categories=list(data.get("categories") or []),
                vertical=data.get("vertical") or "",
                created_at=float(data.get("created_at") or 0),
            )

    rec = latest_plan()
    if rec is None:
        raise ValueError(
            "No plan found. Call plan_leads (or estimate_cost) first, then run_leads()."
        )
    # Soft-warn only: still allow Maps hard-limit blocked plans to surface
    # the BLOCKED estimate, but do not treat as an approval refusal.
    if not rec.plan_path or not Path(rec.plan_path).exists():
        raise ValueError(f"Plan file missing at {rec.plan_path!r}. Re-run plan_leads.")
    return rec


def mark_used(plan_id: str = "") -> None:
    """No-op — plans are reusable. Kept so older call sites import cleanly."""
    del plan_id
    return None


def require_spend_approval(
    *,
    approval_id: str = "",
    plan_path: str = "",
    i_approve_spend: bool = True,
    allow_auto_under: bool = True,
) -> PlanRecord:
    """Legacy alias — resolves a plan, never checks spend approval."""
    del i_approve_spend, allow_auto_under
    return resolve_plan(plan_path=plan_path, plan_id=approval_id)
