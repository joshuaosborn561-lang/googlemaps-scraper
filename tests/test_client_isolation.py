"""Per-client Supabase routing and registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from gmscraper import clients as client_reg
from gmscraper import export, supabase_sync
from gmscraper.store import Store


def test_resolve_aliases() -> None:
    client_reg.load_clients(reload=True)
    assert client_reg.resolve_client("kyle").slug == "peterson"
    assert client_reg.resolve_client("carlos").slug == "basco"
    assert client_reg.resolve_client("peterson").supabase_schema == "public"
    assert client_reg.resolve_client("peterson").leads_table == "peterson_leads"
    assert client_reg.resolve_client("basco").leads_fqn == "public.basco_leads"


def test_unknown_client_raises() -> None:
    client_reg.load_clients(reload=True)
    with pytest.raises(ValueError, match="Unknown client_tag"):
        client_reg.resolve_client("acme_roofing_xyz")


def test_sync_target_requires_client_tag() -> None:
    with pytest.raises(ValueError, match="client_tag is required"):
        supabase_sync.resolve_sync_target()


def test_sync_target_routes_to_client_schema() -> None:
    client_reg.load_clients(reload=True)
    t = supabase_sync.resolve_sync_target(client_tag="kyle")
    assert t["client_tag"] == "peterson"
    assert t["schema"] == "public"
    assert t["table"] == "peterson_leads"
    assert t["fqn"] == "public.peterson_leads"


def test_export_filters_by_client_tag(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    store.upsert_businesses(
        [
            {
                "place_id": "p1",
                "name": "Kyle Biz",
                "domain": "kylebiz.com",
                "state": "TX",
                "client_tag": "peterson",
            },
            {
                "place_id": "c1",
                "name": "Carlos Biz",
                "domain": "carlosbiz.com",
                "state": "NJ",
                "client_tag": "basco",
            },
        ]
    )
    store.save_verdict("p1", True, 0.9, "ok", "test")
    store.save_verdict("c1", True, 0.9, "ok", "test")
    store.queue_sites()
    store.save_site("kylebiz.com", "ok", "text", [], None)
    store.save_site("carlosbiz.com", "ok", "text", [], None)

    kyle = list(export.iter_leads(store, icp_only=True, client_tag="peterson"))
    carlos = list(export.iter_leads(store, icp_only=True, client_tag="basco"))
    assert len(kyle) == 1 and kyle[0]["name"] == "Kyle Biz"
    assert len(carlos) == 1 and carlos[0]["name"] == "Carlos Biz"


def test_export_scopes_untagged_by_state_and_category(tmp_path: Path) -> None:
    """Historical rows without client_tag must be selectable by geo/category."""
    store = Store(str(tmp_path / "t.db"))
    store.upsert_businesses(
        [
            {
                "place_id": "tx_old",
                "name": "Old TX GC",
                "domain": "oldtx.com",
                "state": "TX",
                "city": "Dallas",
                "main_category": "General contractor",
                "latitude": 32.78,
                "longitude": -96.80,
            },
            {
                "place_id": "nj_old",
                "name": "Old NJ Dealer",
                "domain": "oldnj.com",
                "state": "NJ",
                "city": "Clifton",
                "main_category": "Car dealer",
                "latitude": 40.88,
                "longitude": -74.16,
            },
            {
                "place_id": "ny_old",
                "name": "Old NY Dealer",
                "domain": "oldny.com",
                "state": "NY",
                "city": "Brooklyn",
                "main_category": "Used car dealer",
            },
        ]
    )
    for pid in ("tx_old", "nj_old", "ny_old"):
        store.save_verdict(pid, True, 0.9, "ok", "test")

    tx = list(export.iter_leads(store, icp_only=True, state="TX"))
    assert [r["place_id"] for r in tx] == ["tx_old"]

    tri = list(export.iter_leads(store, icp_only=True, state="NJ,NY,CT"))
    assert {r["place_id"] for r in tri} == {"nj_old", "ny_old"}

    dealers = list(
        export.iter_leads(
            store, icp_only=True, state="NJ,NY,CT", main_category="dealer"
        )
    )
    assert {r["place_id"] for r in dealers} == {"nj_old", "ny_old"}


def test_sync_uses_state_scope_not_source_client_tag(tmp_path: Path, monkeypatch) -> None:
    """Scoped sync selects untagged rows and stamps destination client_tag."""
    store = Store(str(tmp_path / "t.db"))
    store.upsert_businesses(
        [
            {
                "place_id": "tx1",
                "name": "Untagged TX",
                "domain": "untaggedtx.com",
                "state": "TX",
                "city": "Dallas",
            },
            {
                "place_id": "nj1",
                "name": "Untagged NJ",
                "domain": "untaggednj.com",
                "state": "NJ",
            },
        ]
    )
    store.save_verdict("tx1", True, 0.9, "ok", "test")
    store.save_verdict("nj1", True, 0.9, "ok", "test")

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key")
    captured: list[list[dict]] = []

    def fake_upsert(table, rows, *, schema="public"):
        captured.append(list(rows))
        return len(rows)

    monkeypatch.setattr(supabase_sync, "upsert_rows", fake_upsert)
    monkeypatch.setattr(supabase_sync, "supabase_config", lambda: {"url": "u", "key": "k"})

    out = supabase_sync.sync_to_supabase(
        store, client_tag="peterson", state="TX", icp_only=True
    )
    assert out["rows_synced"] == 1
    assert out["table"] == "peterson_leads"
    assert out["scope"]["state"] == "TX"
    assert out["scope"]["source_filtered_by_client_tag"] is False
    assert len(captured) == 1
    assert captured[0][0]["place_id"] == "tx1"
    assert captured[0][0]["client_tag"] == "peterson"

    # Unscoped sync still requires SQLite client_tag match → 0 historical rows.
    out2 = supabase_sync.sync_to_supabase(
        store, client_tag="peterson", icp_only=True
    )
    assert out2["rows_synced"] == 0
    assert out2["scope"]["source_filtered_by_client_tag"] is True


def test_row_for_supabase_coerces_empty_permit_count() -> None:
    row = supabase_sync._row_for_supabase(
        {
            "place_id": "x",
            "name": "Biz",
            "reviews": "",
            "permit_count": "",
            "rating": "",
            "in_icp": "no",
            "icp_confidence": "",
        },
        "peterson",
        "2026-01-01T00:00:00Z",
        client_tag="peterson",
    )
    assert row["permit_count"] is None
    assert row["reviews"] is None
    assert row["rating"] is None
    assert row["in_icp"] is False
    assert row["client_tag"] == "peterson"


def test_ensure_sql_contains_both_client_tables() -> None:
    client_reg.load_clients(reload=True)
    sql = client_reg.ensure_sql_all()
    assert "CREATE TABLE IF NOT EXISTS public.peterson_leads" in sql
    assert "CREATE TABLE IF NOT EXISTS public.basco_contacts" in sql
    assert "peterson_companies" in sql


def test_list_clients_public() -> None:
    client_reg.load_clients(reload=True)
    pubs = client_reg.list_clients_public()
    slugs = {p["client_tag"] for p in pubs}
    assert slugs == {"peterson", "basco"}
    by_tag = {p["client_tag"]: p for p in pubs}
    assert by_tag["basco"]["icp_questions"]
    assert "dealership" in by_tag["basco"]["icp_questions"].lower()
    assert by_tag["peterson"]["icp_questions"]
    assert "contractor" in by_tag["peterson"]["icp_questions"].lower()


def test_resolve_classify_brief_uses_client_defaults() -> None:
    client_reg.load_clients(reload=True)
    auto = client_reg.resolve_classify_brief("carlos")
    assert auto["client_tag"] == "basco"
    assert auto["icp"].startswith("1.")
    assert "auto repair" in auto["exclude_categories"]

    override = client_reg.resolve_classify_brief(
        "basco", icp="1. Is this a used-car lot?", exclude_categories="junkyard"
    )
    assert override["icp"] == "1. Is this a used-car lot?"
    assert override["exclude_categories"] == "junkyard"

    kyle = client_reg.resolve_classify_brief("peterson")
    assert "contractor" in kyle["icp"].lower()
    assert kyle["client_tag"] == "peterson"
