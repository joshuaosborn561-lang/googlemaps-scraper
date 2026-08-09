"""Plan resolution without spend-approval gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_server import approvals


def test_resolve_plan_by_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(approvals, "PLANS_INDEX_DIR", tmp_path / "approvals")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"vertical": "hvac"}), encoding="utf-8")

    got = approvals.resolve_plan(plan_path=str(plan))
    assert got.plan_path == str(plan)
    assert got.blocked is False


def test_resolve_plan_latest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(approvals, "PLANS_INDEX_DIR", tmp_path / "approvals")
    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    a = approvals.save_plan_record(
        brief="test",
        plan_path=str(plan),
        requests=10,
        estimated_overage_usd=0.0,
        blocked=False,
        states=["TX"],
        categories=["hvac"],
        vertical="hvac",
    )
    # used flag is ignored — plans are reusable
    approvals.mark_used(a.id)
    got = approvals.resolve_plan()
    assert got.id == a.id


def test_resolve_plan_blocked_still_returns(tmp_path: Path, monkeypatch) -> None:
    """Blocked is informational only — resolve_plan never refuses spend."""
    monkeypatch.setattr(approvals, "PLANS_INDEX_DIR", tmp_path / "approvals")
    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    a = approvals.save_plan_record(
        brief="blocked",
        plan_path=str(plan),
        requests=10,
        estimated_overage_usd=None,
        blocked=True,
        states=[],
        categories=[],
        vertical="x",
    )
    got = approvals.resolve_plan(plan_id=a.id)
    assert got.blocked is True
    assert got.id == a.id


def test_no_plan_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(approvals, "PLANS_INDEX_DIR", tmp_path / "approvals")
    with pytest.raises(ValueError, match="No plan found"):
        approvals.resolve_plan()
