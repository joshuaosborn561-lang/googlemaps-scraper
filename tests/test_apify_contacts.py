"""Apify contact crawl cost gates + OpenAI parse filters + waterfall max_tier."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gmscraper import apify_contacts, waterfall
from gmscraper.store import Store
from gmscraper.vendors.base import EmailHit, PersonHit


def test_estimate_cost_215_domains() -> None:
    # FREE tier: 0.001 start + 0.002/page × domains × pages (default 3)
    assert apify_contacts.estimate_cost_usd(215) == pytest.approx(
        0.001 + 0.002 * 215 * 3
    )
    assert apify_contacts.estimate_cost_usd(215, max_pages_per_site=5) == pytest.approx(
        0.001 + 0.002 * 215 * 5
    )
    # verify_emails does not change estimate (leads-enrichment add-on stays off)
    assert apify_contacts.estimate_cost_usd(
        215, verify_emails=True, max_pages_per_site=3
    ) == pytest.approx(0.001 + 0.002 * 215 * 3)


def test_estimate_only_does_not_start(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(apify_contacts.settings, "apify_token", "")
    monkeypatch.setattr(apify_contacts.settings, "apify_max_cost_usd", 5.0)

    domains = ",".join(f"co{i}.example.com" for i in range(215))
    out = apify_contacts.crawl(
        store, domains=domains, estimate_only=True, verify_emails=False
    )
    assert out["started"] is False
    assert out["blocked"] is False
    assert out["domains"] == 215
    assert out["max_pages_per_site"] == 3
    assert out["estimated_cost_usd"] == pytest.approx(0.001 + 0.002 * 215 * 3)
    assert out.get("run_id") is None
    assert store.apify_contact_raw_rows() == []


def test_cost_ceiling_blocks_run(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(apify_contacts.settings, "apify_token", "tok")
    monkeypatch.setattr(apify_contacts.settings, "apify_max_cost_usd", 0.10)

    with patch.object(apify_contacts, "apify_token_valid", return_value=True):
        out = apify_contacts.crawl(
            store,
            domains=",".join(f"x{i}.test" for i in range(215)),
            estimate_only=False,
        )
    assert out["blocked"] is True
    assert out["started"] is False
    assert out["estimated_cost_usd"] == pytest.approx(0.001 + 0.002 * 215 * 3)


def test_save_apify_contact_raw(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    n = store.save_apify_contact_raw(
        "run123",
        [
            {
                "url": "https://acme.test/team",
                "domain": "acme.test",
                "markdown": "# Team\nJason Parrott, President\njparrott@acme.test",
            }
        ],
        run_label="smoke",
    )
    assert n == 1
    rows = store.apify_contact_raw_rows(run_id="run123")
    assert len(rows) == 1
    assert rows[0]["domain"] == "acme.test"
    payload = json.loads(rows[0]["raw_json"])
    assert "Jason Parrott" in payload["markdown"]


def test_looks_like_person_rejects_junk() -> None:
    assert not apify_contacts._looks_like_person("Project", "Manager")
    assert not apify_contacts._looks_like_person("Dale Construction", "Corporation")
    assert not apify_contacts._looks_like_person("Acme", "Builders LLC")
    assert apify_contacts._looks_like_person("Jason", "Parrott")
    assert apify_contacts._looks_like_person("Steve", "W.")


def test_parse_filters_title_and_company(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "t.db")
    store.save_apify_contact_raw(
        "run-bad",
        [
            {
                "url": "https://junk.test/team",
                "domain": "junk.test",
                "text": "Project Manager\nDale Construction Corporation\nSteve W. Estimator",
            }
        ],
    )

    class FakeLLM:
        model = "gpt-4o-mini"
        spend = {"prompt_tokens": 100, "completion_tokens": 50}

        def json_chat(self, system, prompt, schema):
            return {
                "people": [
                    {
                        "first_name": "Project",
                        "last_name": "Manager",
                        "job_title": "Project Manager",
                        "email": "",
                        "phone": "",
                        "linkedin_url": "",
                        "confidence": 0.9,
                    },
                    {
                        "first_name": "Dale Construction",
                        "last_name": "Corporation",
                        "job_title": "GC",
                        "email": "",
                        "phone": "",
                        "linkedin_url": "",
                        "confidence": 0.8,
                    },
                    {
                        "first_name": "Jason",
                        "last_name": "Parrott",
                        "job_title": "President",
                        "email": "jparrott@junk.test",
                        "phone": "",
                        "linkedin_url": "",
                        "confidence": 0.95,
                    },
                ]
            }

        def spend_line(self):
            return "$0.00"

    monkeypatch.setattr(
        apify_contacts, "make_llm", lambda settings, model="": FakeLLM()
    )
    monkeypatch.setattr(apify_contacts.gc_sync, "upsert_companies", lambda rows: len(rows))
    monkeypatch.setattr(
        apify_contacts.gc_sync, "insert_contacts_ignore_conflict", lambda rows: len(rows)
    )
    monkeypatch.setattr(apify_contacts.gc_sync, "insert_contacts", lambda rows: len(rows))

    out = apify_contacts.parse_contacts_openai(store, run_id="run-bad", workers=1)
    assert out["domains_processed"] == 1
    # Only Jason survives post-filter (email was invented → stripped but person kept)
    assert out["people_extracted"] == 1
    assert out["domains_with_person"] == 1
    contacts = store.contacts_for_domain("junk.test")
    names = {c["name"] for c in contacts}
    assert "Jason Parrott" in names
    assert "Project Manager" not in names
    assert "Dale Construction Corporation" not in names


def test_waterfall_max_tier_blocks_fullenrich(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "t.db")

    gl = MagicMock()
    gl.enabled = True
    gl.calls = 0
    gl.hits = 0
    gl.find_email.return_value = None
    gl.find_people.return_value = []

    lm = MagicMock()
    lm.enabled = True
    lm.calls = 0
    lm.hits = 0
    lm.find_email.return_value = None
    lm.find_people.return_value = []

    fe = MagicMock()
    fe.enabled = True
    fe.calls = 0
    fe.hits = 0
    fe.find_email_bulk.return_value = []
    fe.find_email.return_value = None

    ark = MagicMock()
    ark.enabled = False
    ark.calls = 0
    ark.hits = 0

    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: gl)
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: ark)
    monkeypatch.setattr(waterfall, "LeadMagicClient", lambda: lm)
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: fe)
    monkeypatch.setattr(waterfall.gc_sync, "upsert_companies", lambda rows: len(rows))
    monkeypatch.setattr(
        waterfall.gc_sync, "insert_contacts_ignore_conflict", lambda rows: len(rows)
    )
    monkeypatch.setattr(waterfall.gc_sync, "insert_contacts", lambda rows: len(rows))

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "acme.test",
                "first_name": "Jane",
                "last_name": "Smith",
                "company_name": "Acme",
                "title": "Owner",
            }
        ],
        need="email",
        store=store,
        write_supabase=True,
        max_tier="leadmagic",
        run_apify=False,
    )
    assert out["max_tier"] == "leadmagic"
    fe.find_email_bulk.assert_not_called()
    fe.find_email.assert_not_called()
    assert out["vendors_enabled"]["fullenrich"] is False


def test_waterfall_max_tier_apify_skips_paid_dm(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "t.db")
    store.save_contact(
        name="Jason Parrott",
        domain="acme.test",
        title="President",
        source="apify:contact-info-scraper",
        source_tier="apify_openai",
        confidence=0.9,
    )

    gl = MagicMock()
    gl.enabled = True
    gl.calls = 0
    gl.hits = 0
    gl.find_people.return_value = [
        PersonHit(first_name="Other", last_name="Person", title="CEO", source_tier="getleads")
    ]
    gl.find_email.return_value = None

    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: gl)
    monkeypatch.setattr(
        waterfall, "AiArkClient", lambda: MagicMock(enabled=False, calls=0, hits=0)
    )
    monkeypatch.setattr(
        waterfall, "LeadMagicClient", lambda: MagicMock(enabled=False, calls=0, hits=0)
    )
    monkeypatch.setattr(
        waterfall, "FullEnrichClient", lambda: MagicMock(enabled=False, calls=0, hits=0)
    )
    monkeypatch.setattr(waterfall.gc_sync, "upsert_companies", lambda rows: len(rows))
    monkeypatch.setattr(
        waterfall.gc_sync, "insert_contacts_ignore_conflict", lambda rows: len(rows)
    )
    monkeypatch.setattr(waterfall.gc_sync, "insert_contacts", lambda rows: len(rows))

    out = waterfall.enrich_waterfall(
        [{"domain": "acme.test", "company_name": "Acme"}],
        need="dm",
        store=store,
        write_supabase=True,
        max_tier="apify",
        run_apify=False,  # already have local apify_openai contact
    )
    assert out["dms_found"] == 1
    gl.find_people.assert_not_called()


def test_resolve_email_respects_max_tier() -> None:
    gl = MagicMock(enabled=True, calls=0, hits=0)
    gl.find_email.return_value = None
    lm = MagicMock(enabled=True, calls=0, hits=0)
    lm.find_email.return_value = None
    fe = MagicMock(enabled=True, calls=0, hits=0)
    fe.find_email.return_value = EmailHit(
        email="x@y.com", source_tier="fullenrich", status="valid"
    )

    wf = waterfall.Waterfall(
        getleads=gl, leadmagic=lm, fullenrich=fe, max_tier="leadmagic"
    )
    hit = wf.resolve_email(
        {
            "first_name": "Jane",
            "last_name": "Smith",
            "domain": "acme.test",
            "company_name": "Acme",
            "email": "",
        }
    )
    assert hit is None
    fe.find_email.assert_not_called()

    wf2 = waterfall.Waterfall(
        getleads=gl, leadmagic=lm, fullenrich=fe, max_tier="fullenrich"
    )
    hit2 = wf2.resolve_email(
        {
            "first_name": "Jane",
            "last_name": "Smith",
            "domain": "acme.test",
            "company_name": "Acme",
            "email": "",
        }
    )
    assert hit2 and hit2.email == "x@y.com"
