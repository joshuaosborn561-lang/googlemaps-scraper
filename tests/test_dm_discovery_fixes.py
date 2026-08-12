"""DM discovery fixes: Apify input shape, title rank/reject, name suffixes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from gmscraper import apify_contacts, waterfall
from gmscraper.clients import CONTACTS_DDL, ensure_sql_for_client, resolve_client
from gmscraper.store import Store
from gmscraper.vendors.base import PersonHit, split_name


def test_build_contact_actor_input_social_is_object() -> None:
    payload = apify_contacts.build_contact_actor_input(
        ["https://dealer.example/"], max_pages_per_site=3
    )
    social = payload["scrapeSocialMediaProfiles"]
    assert isinstance(social, dict)
    assert social == {
        "facebooks": False,
        "instagrams": False,
        "youtubes": False,
        "tiktoks": False,
        "twitters": False,
    }
    assert payload["maximumLeadsEnrichmentRecords"] == 0
    assert payload["waitUntil"] == "domcontentloaded"
    assert "leadsEnrichment" not in payload
    apify_contacts.validate_contact_actor_input(payload)


def test_validate_rejects_boolean_social() -> None:
    bad = apify_contacts.build_contact_actor_input(["https://x.test/"])
    bad["scrapeSocialMediaProfiles"] = False  # type: ignore[assignment]
    try:
        apify_contacts.validate_contact_actor_input(bad)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "object" in str(exc).lower()


def test_split_name_strips_suffixes() -> None:
    assert split_name("Leo Karl III") == ("Leo", "Karl")
    assert split_name("Mary Jane Jr.") == ("Mary", "Jane")
    assert split_name("John Smith Sr") == ("John", "Smith")
    assert split_name("Alice Wonder IV") == ("Alice", "Wonder")
    assert split_name("Bob") == ("Bob", "")


def test_title_rank_prefers_service_director() -> None:
    titles = list(waterfall.DEFAULT_DM_TARGET_TITLES)
    assert waterfall._title_rank("Service Director", titles) > waterfall._title_rank(
        "General Manager", titles
    )
    assert waterfall._title_rank("Service Porter", titles) == 0
    assert waterfall._title_rank("Accounting Clerk", titles) == 0
    # short key must not match inside unrelated words
    assert waterfall._title_rank("Segment Lead", titles) == 0
    assert waterfall._title_rank("GM", titles) > 0


def test_dm_rejects_porter_accepts_service_director(monkeypatch) -> None:
    ark = MagicMock(enabled=True, calls=0, hits=0)
    ark.find_people.return_value = [
        PersonHit(
            first_name="Tim",
            last_name="Porter",
            title="Service Porter",
            source_tier="ai_ark",
        ),
        PersonHit(
            first_name="Dana",
            last_name="Mills",
            title="Service Director",
            source_tier="ai_ark",
        ),
    ]
    gl = MagicMock(enabled=True, calls=0, hits=0)
    gl.find_people.return_value = []
    lm = MagicMock(enabled=False, calls=0, hits=0)
    fe = MagicMock(enabled=False, calls=0, hits=0)

    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: gl)
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: ark)
    monkeypatch.setattr(waterfall, "LeadMagicClient", lambda: lm)
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: fe)
    monkeypatch.setattr(waterfall.gc_sync, "upsert_companies", lambda rows, **kw: len(rows))
    monkeypatch.setattr(
        waterfall.gc_sync, "insert_contacts_ignore_conflict", lambda rows, **kw: len(rows)
    )
    monkeypatch.setattr(waterfall.gc_sync, "insert_contacts", lambda rows, **kw: len(rows))

    out = waterfall.enrich_waterfall(
        [{"domain": "dealer.test", "company_name": "Dealer"}],
        need="dm",
        store=None,
        write_supabase=True,
        max_tier="aiark",
        run_apify=False,
        client_tag="basco",
    )
    assert out["dms_found"] == 1
    assert out["target_titles"][0] == "service director"
    # Only Dana (Service Director) counts — Tim the porter is rejected.
    assert out["tier_stats"]["ai_ark"]["dm_hits"] == 1


def test_dm_rejects_all_mismatches_not_a_hit(monkeypatch) -> None:
    ark = MagicMock(enabled=True, calls=0, hits=0)
    ark.find_people.return_value = [
        PersonHit(
            first_name="Ann",
            last_name="Clerk",
            title="Accounting Clerk",
            source_tier="ai_ark",
        ),
    ]
    gl = MagicMock(enabled=True, calls=0, hits=0)
    gl.find_people.return_value = []
    lm = MagicMock(enabled=True, calls=0, hits=0)
    lm.find_people.return_value = []
    fe = MagicMock(enabled=False, calls=0, hits=0)

    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: gl)
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: ark)
    monkeypatch.setattr(waterfall, "LeadMagicClient", lambda: lm)
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: fe)
    monkeypatch.setattr(waterfall.gc_sync, "upsert_companies", lambda rows, **kw: len(rows))
    monkeypatch.setattr(
        waterfall.gc_sync, "insert_contacts_ignore_conflict", lambda rows, **kw: len(rows)
    )
    monkeypatch.setattr(waterfall.gc_sync, "insert_contacts", lambda rows, **kw: len(rows))

    out = waterfall.enrich_waterfall(
        [{"domain": "dealer.test", "company_name": "Dealer"}],
        need="dm",
        store=None,
        write_supabase=True,
        max_tier="leadmagic",
        run_apify=False,
        client_tag="basco",
    )
    assert out["dms_found"] == 0
    assert "title_mismatch" in (out["tier_stats"]["ai_ark"].get("last_skip_reason") or "")


def test_team_page_preferred_over_ai_ark(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "t.db")
    store.save_contact(
        name="Pat Service",
        domain="rooftop.test",
        title="Service Manager",
        source="team_page",
        source_tier="team_page",
        confidence=0.9,
    )

    ark = MagicMock(enabled=True, calls=0, hits=0)
    ark.find_people.return_value = [
        PersonHit(
            first_name="Wrong",
            last_name="Person",
            title="Service Director",
            source_tier="ai_ark",
        )
    ]
    gl = MagicMock(enabled=False, calls=0, hits=0)
    lm = MagicMock(enabled=False, calls=0, hits=0)
    fe = MagicMock(enabled=False, calls=0, hits=0)

    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: gl)
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: ark)
    monkeypatch.setattr(waterfall, "LeadMagicClient", lambda: lm)
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: fe)
    monkeypatch.setattr(waterfall.gc_sync, "upsert_companies", lambda rows, **kw: len(rows))
    monkeypatch.setattr(
        waterfall.gc_sync, "insert_contacts_ignore_conflict", lambda rows, **kw: len(rows)
    )
    monkeypatch.setattr(waterfall.gc_sync, "insert_contacts", lambda rows, **kw: len(rows))

    out = waterfall.enrich_waterfall(
        [{"domain": "rooftop.test", "company_name": "Rooftop"}],
        need="dm",
        store=store,
        write_supabase=True,
        max_tier="aiark",
        run_apify=False,
        client_tag="basco",
    )
    assert out["dms_found"] == 1
    assert out["tier_stats"]["team_page"]["dm_hits"] == 1
    ark.find_people.assert_not_called()


def test_contacts_ddl_has_non_partial_unique() -> None:
    assert "domain_email_key" in CONTACTS_DDL
    assert "WHERE email IS NOT NULL" not in CONTACTS_DDL
    sql = ensure_sql_for_client(resolve_client("basco"))
    assert "basco_contacts_domain_email_key" in sql
    assert "UNIQUE INDEX IF NOT EXISTS basco_contacts_domain_email_key" in sql
