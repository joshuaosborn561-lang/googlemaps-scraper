"""resolve_via_serp cost model, extract, and writeback."""

from __future__ import annotations

from unittest.mock import MagicMock

from gmscraper import resolve_serp as rs
from gmscraper import source_binding as sb


def test_estimate_cost_batching() -> None:
    assert rs.estimate_cost_usd(0) == 0.0
    assert rs.estimate_cost_usd(1) == rs.COST_START_USD + rs.COST_SERP_USD
    assert rs.estimate_cost_usd(100) == rs.COST_START_USD + 100 * rs.COST_SERP_USD
    # Second batch adds another start fee.
    assert rs.estimate_cost_usd(101) == (
        2 * rs.COST_START_USD + 101 * rs.COST_SERP_USD
    )


def test_normalize_website_strips_aggregators() -> None:
    assert rs._normalize_website("https://www.weitzman.com/team") == (
        "https://weitzman.com/team"
    )
    assert rs._normalize_website("https://www.yelp.com/biz/foo") == ""
    assert rs._normalize_website("loopnet.com/listing/1") == ""


def test_extract_business_maps_llm_json() -> None:
    llm = MagicMock()
    llm.json_chat.return_value = {
        "company_name": "Weitzman",
        "website": "https://www.weitzman.com",
        "domain": "weitzman.com",
        "phone": "(214) 555-0100",
        "officer_name": "Jane Officer",
        "confidence": 0.91,
    }
    organic = [
        {
            "title": "Weitzman — 3102 Maple",
            "snippet": "Commercial real estate. Call (214) 555-0100",
            "url": "https://www.weitzman.com",
        }
    ]
    out = rs.extract_business(
        llm, address="3102 MAPLE AVE STE 500, DALLAS TX", organic=organic
    )
    assert out["company_name"] == "Weitzman"
    assert out["domain"] == "weitzman.com"
    assert out["website"] == "https://weitzman.com"
    assert "214" in out["phone"]
    assert out["officer_name"] == "Jane Officer"
    assert out["confidence"] == 0.91


def test_match_item_to_query() -> None:
    items = [
        {"searchQuery": {"term": "other"}, "organicResults": []},
        {
            "searchQuery": {"term": "3102 MAPLE AVE STE 500, DALLAS TX"},
            "organicResults": [{"title": "Weitzman", "url": "https://weitzman.com"}],
        },
    ]
    hit = rs._match_item_to_query(items, "3102 MAPLE AVE STE 500, DALLAS TX")
    assert hit is not None
    assert hit["organicResults"][0]["title"] == "Weitzman"


def test_serp_pending_where_includes_maps_empty() -> None:
    binding = sb.SourceBinding(
        project_id="kemvx",
        schema="permit_parcel",
        table="operators",
        key_column="operator_address",
        address_column="operator_address",
        name_column="operator_name",
        domain_column="domain",
        resolved_column="resolved",
    )
    where = rs.serp_pending_where(binding)
    assert "resolved = false" in where
    assert "business_name" in where
    assert "via" in where
    assert "serp" in where


def test_run_writes_hit_and_marks_resolved(monkeypatch) -> None:
    binding = sb.SourceBinding(
        project_id="kemvx",
        schema="permit_parcel",
        table="operators",
        key_column="operator_address",
        address_column="operator_address",
        name_column="operator_name",
        domain_column="domain",
        resolved_column="resolved",
        confidence_column="confidence",
        supabase_url="http://x",
        supabase_key="k",
    )
    row = {
        "operator_address": "3102 MAPLE AVE STE 500, DALLAS TX",
        "operator_name": None,
        "resolved": True,  # Maps already marked resolved with empty writeback
        "business_name": None,
        "domain": None,
    }
    patches: list[tuple] = []

    monkeypatch.setattr(rs.sb, "resolve_binding", lambda **kw: binding)
    monkeypatch.setattr(rs.sb, "validate_binding", lambda b: None)
    monkeypatch.setattr(rs.sb, "ensure_writeback_columns", lambda b: {"ok": True})
    monkeypatch.setattr(rs, "count_serp_pending", lambda b: 1)
    calls = {"n": 0}

    def _fetch(b, limit=100, offset=0):
        calls["n"] += 1
        return [row] if calls["n"] == 1 else []

    monkeypatch.setattr(rs, "fetch_serp_pending", _fetch)
    monkeypatch.setattr(
        rs.sb,
        "patch_row",
        lambda b, key, patch: patches.append((key, patch)),
    )
    monkeypatch.setattr(rs.settings, "apify_token", "apify-token")
    monkeypatch.setattr(rs.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(rs.settings, "apify_max_cost_usd", 5.0)

    llm = MagicMock()
    llm.json_chat.return_value = {
        "company_name": "Weitzman",
        "website": "https://weitzman.com",
        "domain": "weitzman.com",
        "phone": "214-555-0100",
        "officer_name": "Jane Officer",
        "confidence": 0.9,
    }
    monkeypatch.setattr(rs, "make_llm", lambda s: llm)
    monkeypatch.setattr(
        rs,
        "run_serp_batch",
        lambda queries, max_cost=5.0: {
            "items": [
                {
                    "searchQuery": {"term": queries[0]},
                    "organicResults": [
                        {
                            "title": "Weitzman",
                            "description": "CRE firm",
                            "url": "https://weitzman.com",
                        }
                    ],
                }
            ],
            "usage_usd": 0.0055,
            "run_id": "run1",
            "status": "SUCCEEDED",
        },
    )

    out = rs.run(
        schema="permit_parcel",
        table="operators",
        key_column="operator_address",
        address_column="operator_address",
        name_column="operator_name",
        limit=1,
        min_confidence=0.35,
    )
    assert out["started"] is True
    assert out["hit"] == 1
    assert out["resolved"] == 1
    assert patches
    key, patch = patches[0]
    assert key == row["operator_address"]
    assert patch["resolved"] is True
    assert patch["operator_name"] == "Weitzman"
    assert patch["business_name"] == "Weitzman"
    assert patch["domain"] == "weitzman.com"
    assert patch["website"] == "https://weitzman.com"
    assert "214" in patch["phone"]
    assert patch["confidence"] >= 0.35


def test_estimate_only_no_spend(monkeypatch) -> None:
    binding = sb.SourceBinding(
        project_id="kemvx",
        schema="permit_parcel",
        table="operators",
        key_column="operator_address",
        address_column="operator_address",
        supabase_url="http://x",
        supabase_key="k",
    )
    monkeypatch.setattr(rs.sb, "resolve_binding", lambda **kw: binding)
    monkeypatch.setattr(rs.sb, "validate_binding", lambda b: None)
    monkeypatch.setattr(rs, "count_serp_pending", lambda b: 50)
    monkeypatch.setattr(rs.settings, "apify_max_cost_usd", 5.0)

    out = rs.run(
        schema="permit_parcel",
        table="operators",
        key_column="operator_address",
        estimate_only=True,
        limit=50,
    )
    assert out["estimate_only"] is True
    assert out["started"] is False
    assert out["pending_rows"] == 50
    assert out["estimated_cost_usd"] == round(rs.estimate_cost_usd(50), 4)
