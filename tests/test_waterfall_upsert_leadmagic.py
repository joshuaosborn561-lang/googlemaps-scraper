"""Company upsert dedupe + LeadMagic people-search wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from gmscraper import gc_sync, waterfall
from gmscraper.vendors import leadmagic
from gmscraper.vendors.base import PersonHit


def test_dedupe_companies_by_domain_merges_and_prefers_found() -> None:
    rows = [
        gc_sync.company_row(
            domain="Dealer.Example",
            company_name="Dealer A",
            dm_lookup_status="not_found",
        ),
        gc_sync.company_row(
            domain="dealer.example",
            company_name="",
            dm_lookup_status="found",
            dm_source_tier="leadmagic",
        ),
        gc_sync.company_row(
            domain="other.test",
            company_name="Other",
            dm_lookup_status="not_found",
        ),
    ]
    out = gc_sync.dedupe_companies_by_domain(rows)
    assert len(out) == 2
    by = {r["domain"]: r for r in out}
    assert by["dealer.example"]["dm_lookup_status"] == "found"
    assert by["dealer.example"]["dm_source_tier"] == "leadmagic"
    assert by["dealer.example"]["company_name"] == "Dealer A"
    assert by["other.test"]["company_name"] == "Other"


def test_upsert_companies_dedupes_before_post(monkeypatch) -> None:
    seen: list[list] = []

    def fake_request(method, path, key, base, *, body=None, prefer="", schema=""):
        seen.append(body)
        return 201, ""

    monkeypatch.setattr(gc_sync, "_request", fake_request)
    monkeypatch.setattr(
        gc_sync,
        "supabase_config",
        lambda: {"url": "https://example.supabase.co", "key": "k"},
    )
    monkeypatch.setattr(
        gc_sync,
        "resolve_write_schema",
        lambda client_tag="", schema="": {
            "schema": "public",
            "client_tag": "basco",
            "companies_table": "basco_companies",
            "contacts_table": "basco_contacts",
        },
    )

    n = gc_sync.upsert_companies(
        [
            {"domain": "a.test", "company_name": "A1", "dm_lookup_status": "not_found"},
            {"domain": "a.test", "company_name": "A2", "dm_lookup_status": "found"},
            {"domain": "b.test", "company_name": "B"},
        ],
        client_tag="basco",
    )
    assert n == 2
    assert len(seen) == 1
    domains = [r["domain"] for r in seen[0]]
    assert domains == ["a.test", "b.test"]
    assert seen[0][0]["dm_lookup_status"] == "found"


def test_leadmagic_find_people_uses_v3_search_with_titles() -> None:
    client = leadmagic.LeadMagicClient(api_key="test-key")

    class Resp:
        status_code = 200

        def json(self):
            return {
                "people": [
                    {
                        "contact_first_name": "Dana",
                        "contact_last_name": "Mills",
                        "contact_full_name": "Dana Mills",
                        "contact_job_title": "Service Director",
                        "contact_linkedin_url": "https://linkedin.com/in/dana",
                    }
                ]
            }

    with patch.object(leadmagic.requests, "post", return_value=Resp()) as post:
        people = client.find_people(
            "paragonhonda.com",
            titles=["service director", "service manager", "gm"],
            limit=5,
        )

    assert len(people) == 1
    assert people[0].first_name == "Dana"
    assert people[0].title == "Service Director"
    assert post.call_args.args[0].endswith("/v3/people/search")
    body = post.call_args.kwargs["json"]
    assert body["company_domain"] == "paragonhonda.com"
    assert body["titles"] == ["Service Director", "Service Manager", "GM"]
    assert body["include_contact_details"] is False
    assert "role-finder" not in post.call_args.args[0]


def test_leadmagic_find_people_skips_without_titles() -> None:
    client = leadmagic.LeadMagicClient(api_key="test-key")
    with patch.object(leadmagic.requests, "post") as post:
        assert client.find_people("x.com", titles=None) == []
        assert client.find_people("x.com", titles=[]) == []
    post.assert_not_called()


def test_waterfall_passes_titles_to_leadmagic(monkeypatch) -> None:
    ark = MagicMock(enabled=True, calls=0, hits=0)
    ark.find_people.return_value = []
    gl = MagicMock(enabled=True, calls=0, hits=0)
    gl.find_people.return_value = []
    lm = MagicMock(enabled=True, calls=0, hits=0)
    lm.find_people.return_value = [
        PersonHit(
            first_name="Pat",
            last_name="Service",
            title="Service Manager",
            source_tier="leadmagic",
        )
    ]
    fe = MagicMock(enabled=False, calls=0, hits=0)

    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: gl)
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: ark)
    monkeypatch.setattr(waterfall, "LeadMagicClient", lambda: lm)
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: fe)
    def upsert_deduped(rows, **kw):
        return len(gc_sync.dedupe_companies_by_domain(rows))

    monkeypatch.setattr(waterfall.gc_sync, "upsert_companies", upsert_deduped)
    monkeypatch.setattr(
        waterfall.gc_sync, "insert_contacts_ignore_conflict", lambda rows, **kw: len(rows)
    )
    monkeypatch.setattr(waterfall.gc_sync, "insert_contacts", lambda rows, **kw: len(rows))

    # Same domain twice — previously crashed companies upsert with 21000.
    out = waterfall.enrich_waterfall(
        [
            {"domain": "paragonhonda.com", "company_name": "Paragon Honda"},
            {"domain": "paragonhonda.com", "company_name": "Paragon Honda"},
        ],
        need="dm",
        store=None,
        write_supabase=True,
        max_tier="leadmagic",
        run_apify=False,
        client_tag="basco",
    )
    assert out["dms_found"] == 2
    assert out["companies_upserted"] == 1  # deduped at upsert
    assert lm.find_people.called
    args = lm.find_people.call_args
    assert "titles" in args.kwargs
    assert any("service" in t.lower() for t in args.kwargs["titles"])


def test_enrich_waterfall_duplicate_domain_does_not_crash(monkeypatch) -> None:
    """Duplicate domains: upsert_companies receives many rows but posts one per domain."""
    posted: list[list] = []

    def fake_request(method, path, key, base, *, body=None, prefer="", schema=""):
        posted.append(body or [])
        return 201, ""

    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: MagicMock(enabled=False, calls=0, hits=0))
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: MagicMock(enabled=False, calls=0, hits=0))
    monkeypatch.setattr(
        waterfall, "LeadMagicClient", lambda: MagicMock(enabled=False, calls=0, hits=0)
    )
    monkeypatch.setattr(
        waterfall, "FullEnrichClient", lambda: MagicMock(enabled=False, calls=0, hits=0)
    )
    monkeypatch.setattr(gc_sync, "_request", fake_request)
    monkeypatch.setattr(
        gc_sync,
        "supabase_config",
        lambda: {"url": "https://example.supabase.co", "key": "k"},
    )
    # Use real upsert (with dedupe) via the waterfall module binding.
    monkeypatch.setattr(waterfall.gc_sync, "upsert_companies", gc_sync.upsert_companies)
    monkeypatch.setattr(
        waterfall.gc_sync, "insert_contacts_ignore_conflict", lambda rows, **kw: 0
    )
    monkeypatch.setattr(waterfall.gc_sync, "insert_contacts", lambda rows, **kw: 0)
    monkeypatch.setattr(
        gc_sync,
        "resolve_write_schema",
        lambda client_tag="", schema="": {
            "schema": "public",
            "client_tag": "basco",
            "companies_table": "basco_companies",
            "contacts_table": "basco_contacts",
        },
    )

    out = waterfall.enrich_waterfall(
        [
            {"domain": "a.test", "company_name": "A"},
            {"domain": "a.test", "company_name": "A again"},
            {"domain": "b.test", "company_name": "B"},
        ],
        need="dm",
        store=None,
        write_supabase=True,
        max_tier="apify",
        run_apify=False,
        client_tag="basco",
        require_title_match=False,
    )
    assert out["rows_in"] == 3
    assert out["companies_upserted"] == 2
    assert len(posted) == 1
    domains = [r["domain"] for r in posted[0]]
    assert domains == ["a.test", "b.test"]
