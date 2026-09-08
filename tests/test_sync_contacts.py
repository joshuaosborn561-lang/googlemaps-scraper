"""dataset='contacts' sync + unscoped owner backfill on leads."""

from __future__ import annotations

from pathlib import Path

import pytest

from gmscraper import supabase_sync
from gmscraper.store import Store


def _store_with_people(tmp_path: Path) -> Store:
    store = Store(str(tmp_path / "t.db"))
    store.upsert_businesses(
        [
            {
                "place_id": "pete1",
                "name": "DFW Property",
                "domain": "dfwpm.com",
                "city": "Dallas",
                "state": "TX",
                "client_tag": "peterson",
                "plan_id": "kyle-plan",
            },
            {
                "place_id": "pete2",
                "name": "Other DFW",
                "domain": "otherdfw.com",
                "city": "Fort Worth",
                "state": "TX",
                "client_tag": "peterson",
                "plan_id": "other-plan",
            },
            {
                "place_id": "basco1",
                "name": "Brooklyn Cadillac",
                "domain": "bkcadillac.com",
                "city": "Brooklyn",
                "state": "NY",
                "client_tag": "basco",
                "plan_id": "carlos-plan",
            },
        ]
    )
    store.save_contact(
        name="Kyle Manager",
        domain="dfwpm.com",
        place_id="pete1",
        title="Property Manager",
        email="kyle@dfwpm.com",
        source="team_page",
        source_tier="team_page",
        source_url="https://dfwpm.com/team",
        confidence=0.9,
    )
    store.save_contact(
        name="Pat Owner",
        domain="otherdfw.com",
        place_id="pete2",
        title="Owner",
        source="website",
        source_url="https://otherdfw.com/about",
        confidence=0.8,
    )
    store.save_contact(
        name="Carlos Service",
        domain="bkcadillac.com",
        place_id="basco1",
        title="Service Director",
        source="team_page",
        source_url="https://bkcadillac.com/team",
        confidence=0.7,
    )
    store.save_owner("pete1", "Kyle Manager", "Property Manager", "team_page", 0.9, "test")
    store.save_owner("pete2", "Pat Owner", "Owner", "website", 0.8, "test")
    store.save_owner("basco1", "Carlos Service", "Service Director", "team_page", 0.7, "test")
    return store


def test_split_name_and_source_tool() -> None:
    assert supabase_sync.split_name("Kyle Manager") == ("Kyle", "Manager")
    assert supabase_sync.split_name("Madonna") == ("Madonna", "")
    assert supabase_sync.source_tool_for("team_page") == "extract_team_contacts"
    assert supabase_sync.source_tool_for("website") == "find_owners"
    key = supabase_sync.natural_key(
        client_tag="peterson",
        domain="dfwpm.com",
        first_name="Kyle",
        last_name="Manager",
        email="kyle@dfwpm.com",
    )
    assert key == "n|peterson|dfwpm.com|kyle|manager"
    email_key = supabase_sync.natural_key(
        client_tag="peterson",
        domain="dfwpm.com",
        first_name="Madonna",
        last_name="",
        email="x@dfwpm.com",
    )
    assert email_key == "e|peterson|dfwpm.com|x@dfwpm.com"


def test_contacts_sync_filters_by_client_tag(tmp_path: Path, monkeypatch) -> None:
    store = _store_with_people(tmp_path)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key")
    captured: list[list[dict]] = []

    def fake_upsert(table, rows, *, schema="public", on_conflict="place_id,run_label"):
        captured.append(list(rows))
        assert table == "contacts"
        assert schema == "client_peterson"
        assert on_conflict == "natural_key"
        return len(rows)

    monkeypatch.setattr(supabase_sync, "upsert_rows", fake_upsert)
    monkeypatch.setattr(supabase_sync, "supabase_config", lambda: {"url": "u", "key": "k"})
    monkeypatch.setattr(supabase_sync, "_existing_natural_keys", lambda *a, **k: set())

    out = supabase_sync.sync_to_supabase(
        store, client_tag="peterson", dataset="contacts"
    )
    assert out["dataset"] == "contacts"
    assert out["fqn"] == "client_peterson.contacts"
    assert out["rows_synced"] == 2
    assert out["rows_skipped"] == 0
    assert "items" not in out and "rows" not in out
    assert "verify_sql" in out and "client_peterson.contacts" in out["verify_sql"]
    names = {r["raw_name"] for r in captured[0]}
    assert names == {"Kyle Manager", "Pat Owner"}
    assert all(r["client_tag"] == "peterson" for r in captured[0])
    by_name = {r["raw_name"]: r for r in captured[0]}
    assert by_name["Kyle Manager"]["first_name"] == "Kyle"
    assert by_name["Kyle Manager"]["last_name"] == "Manager"
    assert by_name["Kyle Manager"]["source_tool"] == "extract_team_contacts"
    assert by_name["Kyle Manager"]["source_url"] == "https://dfwpm.com/team"
    assert by_name["Kyle Manager"]["place_id"] == "pete1"
    assert by_name["Pat Owner"]["source_tool"] == "find_owners"
    assert "Carlos Service" not in names


def test_contacts_pagination_resume_token(tmp_path: Path, monkeypatch) -> None:
    store = _store_with_people(tmp_path)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key")
    monkeypatch.setattr(supabase_sync, "upsert_rows", lambda *a, **k: len(k.get("rows") or a[1]))
    monkeypatch.setattr(supabase_sync, "supabase_config", lambda: {"url": "u", "key": "k"})
    monkeypatch.setattr(supabase_sync, "_existing_natural_keys", lambda *a, **k: set())

    page1 = supabase_sync.sync_to_supabase(
        store,
        client_tag="peterson",
        dataset="contacts",
        page_size=1,
        max_pages=1,
    )
    assert page1["rows_synced"] == 1
    assert page1["has_more"] is True
    assert page1["resume_token"] == "1"

    page2 = supabase_sync.sync_to_supabase(
        store,
        client_tag="peterson",
        dataset="contacts",
        cursor=int(page1["resume_token"]),
        page_size=1,
        max_pages=1,
    )
    assert page2["rows_synced"] == 1
    page3 = supabase_sync.sync_to_supabase(
        store,
        client_tag="peterson",
        dataset="contacts",
        cursor=int(page2["resume_token"]),
        page_size=1,
        max_pages=1,
    )
    assert page3["rows_synced"] == 0
    assert page3["has_more"] is False


def test_leads_owner_backfill_ignores_plan_scope(tmp_path: Path, monkeypatch) -> None:
    store = _store_with_people(tmp_path)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key")
    owners: list[dict] = []

    def fake_backfill(store_arg, *, schema, table):
        assert schema == "client_peterson"
        assert table == "leads"
        rows = supabase_sync.iter_local_owners(store_arg)
        owners.extend(rows)
        return len(rows)

    monkeypatch.setattr(supabase_sync, "backfill_lead_owners", fake_backfill)
    monkeypatch.setattr(supabase_sync, "upsert_rows", lambda *a, **k: len(a[1]))
    monkeypatch.setattr(supabase_sync, "supabase_config", lambda: {"url": "u", "key": "k"})

    out = supabase_sync.sync_to_supabase(
        store, client_tag="peterson", plan_id="kyle-plan", icp_only=False
    )
    # Plan scope syncs one lead, but owners from every local row are backfilled.
    assert out["rows_synced"] == 1
    assert out["owners_backfilled"] == 3
    assert {r["place_id"] for r in owners} == {"pete1", "pete2", "basco1"}
    assert out["verify_sql"].endswith("client_peterson.leads;")


def test_truncate_refused(tmp_path: Path, monkeypatch) -> None:
    store = Store(str(tmp_path / "t.db"))
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key")
    with pytest.raises(ValueError, match="truncate is disabled"):
        supabase_sync.sync_to_supabase(store, client_tag="peterson", truncate=True)
