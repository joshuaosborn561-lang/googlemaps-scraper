"""P0/P1 acceptance coverage: details_only, geo gate, errors, AI Ark skips."""

from __future__ import annotations

from unittest.mock import MagicMock

from gmscraper import classify, resolve_places, team_contacts, waterfall
from gmscraper import source_binding as sb
from mcp_server.errors import ToolError, classify_exception, tool_error_from_exception


def test_details_only_one_row_backfill(monkeypatch) -> None:
    binding = sb.SourceBinding(
        project_id="x",
        schema="s",
        table="t",
        key_column="id",
        address_column="addr",
        domain_column="domain",
        resolved_column="resolved",
        confidence_column="confidence",
        supabase_url="http://x",
        supabase_key="k",
    )
    patches: list[dict] = []
    monkeypatch.setattr(
        resolve_places.sb, "patch_row", lambda b, key, patch: patches.append(patch)
    )
    client = MagicMock()
    client.place_details.return_value = {
        "place_id": "ChIJabc",
        "website": "https://www.acme.com/about",
        "phone": "+12145550100",
        "name": "Acme",
    }
    row = {
        "id": 7,
        "place_id": "ChIJabc",
        "website": "",
        "resolved": True,
        "confidence": 0.9,
    }
    out = resolve_places.details_only_one_row(client, binding, row)
    assert out["status"] == "details_ok"
    assert out["domain"] == "acme.com"
    assert out["website"] == "https://www.acme.com/about"
    assert out["phone"] == "+12145550100"
    client.place_details.assert_called_once_with("ChIJabc")
    assert patches[-1]["website"] == "https://www.acme.com/about"
    assert patches[-1]["resolved"] is True


def test_details_only_where_ignores_resolved() -> None:
    binding = sb.SourceBinding(
        schema="s",
        table="t",
        key_column="id",
        resolved_column="resolved",
    )
    where = sb.details_only_where(binding)
    assert "place_id IS NOT NULL" in where
    assert "website" in where
    assert "resolved" not in where  # must ignore resolved flag


def test_tool_error_never_approval_kind() -> None:
    err = tool_error_from_exception(RuntimeError("No approval received"))
    assert err["ok"] is False
    assert err["error"]["kind"] == "internal_error"
    assert "request_id" in err["error"]
    assert "No approval received" not in err["error"]["message"]
    assert classify_exception(ValueError("bad")) == "bad_arguments"
    te = ToolError("token bad", kind="missing_or_invalid_credential")
    assert te.to_dict()["error"]["kind"] == "missing_or_invalid_credential"


def test_ai_ark_skip_reason_when_email_with_names(monkeypatch) -> None:
    monkeypatch.setenv("AI_ARK_API_KEY", "test-key")
    for key in (
        "GETLEADS_API_KEY",
        "LEADMAGIC_API_KEY",
        "FULLENRICH_API_KEY",
        "APIFY_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    rows = [
        {
            "domain": "acme.com",
            "first_name": "Jane",
            "last_name": "Doe",
            "company_name": "Acme",
            "email": "",
            "title": "Owner",
            "full_name": "Jane Doe",
        }
    ]
    out = waterfall.enrich_waterfall(
        rows,
        need="email",
        store=None,
        write_supabase=False,
        run_apify=False,
        max_tier="leadmagic",
    )
    stats = out["tier_stats"]["ai_ark"]
    assert stats.get("calls", 0) == 0
    assert "people_discovery_only" in (stats.get("last_skip_reason") or "")
    assert out["vendors_enabled"]["ai_ark"] is True


def test_looks_like_person_rejects_title_company_truncated() -> None:
    assert not team_contacts.looks_like_person("Project", "Manager")
    assert not team_contacts.looks_like_person("Dale", "Construction Corporation")
    assert not team_contacts.looks_like_person("Steve", "W.")
    assert team_contacts.looks_like_person("Jane", "Doe")


def test_geo_gate_rejects_outside_radius(tmp_path, monkeypatch) -> None:
    from gmscraper.store import Store

    db = tmp_path / "t.db"
    store = Store(str(db))
    # Inside: Dallas-ish
    store.conn.execute(
        "INSERT INTO businesses (place_id, name, domain, latitude, longitude, source) "
        "VALUES (?,?,?,?,?,?)",
        ("in1", "In Radius", "in.example", 32.78, -96.80, "maps"),
    )
    # Outside: LA
    store.conn.execute(
        "INSERT INTO businesses (place_id, name, domain, latitude, longitude, source) "
        "VALUES (?,?,?,?,?,?)",
        ("out1", "Out Radius", "out.example", 34.05, -118.25, "maps"),
    )
    store.conn.execute(
        "INSERT INTO sites (domain, status, text) VALUES (?,?,?)",
        ("in.example", "ok", "We install HVAC systems for homes."),
    )
    store.conn.execute(
        "INSERT INTO sites (domain, status, text) VALUES (?,?,?)",
        ("out.example", "ok", "We install HVAC systems for homes."),
    )
    store.conn.commit()

    class FakeLLM:
        model = "fake"

        def json_chat(self, system, prompt, schema):
            return {"in_icp": True, "confidence": 0.9, "reason": "matches"}

        def spend_line(self):
            return None

    out = classify.run(
        store,
        FakeLLM(),  # type: ignore[arg-type]
        "HVAC contractors",
        workers=1,
        center_lat=32.78,
        center_lng=-96.80,
        radius_miles=40,
        require_geo=True,
        client_tag="basco",
    )
    assert out["geo_rejected"] == 1
    assert out["in_icp"] == 1
    # Outside row must not be in_icp
    row = store.conn.execute(
        "SELECT in_icp, reason FROM business_icp "
        "WHERE place_id='out1' AND client_tag='basco'"
    ).fetchone()
    assert row["in_icp"] == 0
    assert "outside_radius" in (row["reason"] or "")
