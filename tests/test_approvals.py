"""Plan resolution without spend-approval gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_server import approvals


def test_resolve_plan_by_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(approvals, "APPROVALS_DIR", tmp_path / "approvals")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"vertical": "hvac"}), encoding="utf-8")

    got = approvals.resolve_plan(plan_path=str(plan))
    assert got.plan_path == str(plan)
    assert got.blocked is False


def test_resolve_plan_latest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(approvals, "APPROVALS_DIR", tmp_path / "approvals")
    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    a = approvals.create_approval(
        brief="test",
        plan_path=str(plan),
        requests=10,
        estimated_overage_usd=0.0,
        blocked=False,
        states=["TX"],
        categories=["hvac"],
        vertical="hvac",
    )
    # used + expired still accepted
    approvals.mark_used(a.id)
    got = approvals.resolve_plan()
    assert got.id == a.id
    assert got.used is True


def test_resolve_plan_blocked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(approvals, "APPROVALS_DIR", tmp_path / "approvals")
    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    a = approvals.create_approval(
        brief="blocked",
        plan_path=str(plan),
        requests=10,
        estimated_overage_usd=None,
        blocked=True,
        states=[],
        categories=[],
        vertical="x",
    )
    with pytest.raises(ValueError, match="BLOCKED"):
        approvals.resolve_plan(approval_id=a.id)


def test_no_plan_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(approvals, "APPROVALS_DIR", tmp_path / "approvals")
    with pytest.raises(ValueError, match="No plan found"):
        approvals.resolve_plan()
