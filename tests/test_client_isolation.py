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
