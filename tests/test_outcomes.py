"""Outcome-oriented MCP orchestration."""

from __future__ import annotations

from unittest.mock import MagicMock

from gmscraper import outcomes as oc
from mcp_server import server


def test_parse_addresses_newline_and_json() -> None:
    assert oc.parse_addresses("a\nb\n") == ["a", "b"]
    assert oc.parse_addresses('["x", "y"]') == ["x", "y"]
    assert oc.parse_addresses("") == []


def test_resolve_addresses_requires_input() -> None:
    out = oc.resolve_addresses()
    assert out["outcome"] == "nothing_to_do"
    assert out["started"] is False


def test_resolve_addresses_estimate_ad_hoc() -> None:
    out = oc.resolve_addresses(
        addresses="3102 MAPLE AVE STE 500, DALLAS TX\n100 MAIN ST, AUSTIN TX",
        method="serp",
        estimate_only=True,
    )
    assert out["estimate_only"] is True
    assert out["address_count"] == 2
    assert out["estimated_serp_cost_usd"] > 0


def test_resolve_addresses_ad_hoc_serp(monkeypatch) -> None:
    monkeypatch.setattr(oc.settings, "apify_token", "t")
    monkeypatch.setattr(oc.settings, "openai_api_key", "sk")
    monkeypatch.setattr(oc.settings, "apify_max_cost_usd", 5.0)
    monkeypatch.setattr(
        oc,
        "_serp_lookup_batch",
        lambda addresses, min_confidence=0.35: [
            {
                "address": addresses[0],
                "method": "serp",
                "status": "useful",
                "confidence": 0.9,
                "business_name": "Weitzman",
                "domain": "weitzmangroup.com",
                "website": "https://weitzmangroup.com",
                "phone": "214",
                "officer_name": "",
            }
        ],
    )
    out = oc.resolve_addresses(
        addresses="3102 MAPLE AVE STE 500, DALLAS TX",
        method="serp",
        estimate_only=False,
    )
    assert out["started"] is True
    assert out["useful_with_domain"] == 1
    assert out["outcome"] == "ok"
    assert out["results"][0]["business_name"] == "Weitzman"


def test_run_owner_lane_dry_rebuild_stops_before_resolve(monkeypatch) -> None:
    monkeypatch.setattr(
        oc.ops,
        "build_operators",
        lambda **kw: {
            "dry_run": True,
            "operators": 100,
            "min_parcels": kw.get("min_parcels", 1),
            "geo": {"radius_miles": kw.get("radius_miles") or None},
            "top_operators_sample": [],
            "started": False,
            "operators_built": 0,
        },
    )

    def _boom(**kw):
        raise AssertionError("resolve must not run after dry rebuild")

    monkeypatch.setattr(oc, "resolve_addresses", _boom)
    monkeypatch.setattr(oc, "status", _boom)
    out = oc.run_owner_lane(
        states="TX",
        rebuild_operators=True,
        operators_dry_run=True,
        min_parcels=2,
        center="Dallas, TX",
        radius_miles=60,
        estimate_only=True,
    )
    assert out["outcome"] in ("needs_confirm", "estimate")
    assert out["started"] is False
    assert "resolve" not in out["per_stage"]
    assert out["per_stage"]["build_operators"]["operators"] == 100


def test_primary_tools_in_playbook() -> None:
    from mcp_server.playbook import INSTRUCTIONS

    assert "resolve_addresses" in INSTRUCTIONS
    assert "run_owner_lane" in INSTRUCTIONS
    assert "run_lead_list" in INSTRUCTIONS
    assert "outcome_status" in INSTRUCTIONS
    assert "You are NOT the orchestrator" in INSTRUCTIONS


def test_outcome_status_tool_callable(monkeypatch) -> None:
    import gmscraper.outcomes as real

    monkeypatch.setattr(
        real,
        "status",
        lambda **kw: {"scope": "operators", "outcome": "ok", "inventory": {"total_rows": 1}},
    )
    out = server.outcome_status(scope="operators")
    assert "operators" in out
