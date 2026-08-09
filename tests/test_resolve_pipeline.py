"""Generic resolve_places, source binding, LLM extract filters, waterfall max_tier."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gmscraper import resolve_places, team_contacts, waterfall
from gmscraper.vendors.base import EmailHit, PersonHit


def test_address_confidence_suite_mismatch() -> None:
    query = "3102 MAPLE AVE STE 500, DALLAS TX 75201"
    # Wrong tenant in same tower
    result = {
        "name": "Other Tenant LLC",
        "address": "3102 Maple Ave Ste 200, Dallas, TX 75201",
        "zip": "75201",
        "place_id": "x1",
    }
    score = resolve_places.score_candidate(query, result)
    assert score < 0.6

    # Strong match
    good = {
        "name": "Acme Partners",
        "address": "3102 Maple Ave Ste 500, Dallas, TX 75201",
        "zip": "75201",
        "place_id": "x2",
    }
    assert resolve_places.score_candidate(query, good) >= 0.6


def test_pick_best_caps_multi_tenant() -> None:
    query = "100 Main St, Dallas TX 75201"
    results = [
        {"name": f"Biz {i}", "address": "100 Main St, Dallas, TX 75201",
         "zip": "75201", "place_id": f"p{i}"}
        for i in range(5)
    ]
    best, conf, n = resolve_places.pick_best(query, results, min_confidence=0.6)
    assert n >= 4
    assert best is None  # capped below min_confidence
    assert conf <= 0.45


def test_virtual_office_penalty() -> None:
    query = "100 Main St, Dallas TX 75201"
    result = {
        "name": "WeWork Dallas",
        "address": "100 Main St, Dallas, TX 75201",
        "zip": "75201",
        "main_category": "coworking space",
        "place_id": "w",
    }
    assert resolve_places.score_candidate(query, result) < 0.5


def test_llm_extract_rejects_junk_names() -> None:
    class FakeLLM:
        model = "gpt-4o-mini"

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
                        "first_name": "Steve",
                        "last_name": "W.",
                        "job_title": "Estimator",
                        "email": "",
                        "phone": "",
                        "linkedin_url": "",
                        "confidence": 0.4,
                    },
                    {
                        "first_name": "Jason",
                        "last_name": "Parrott",
                        "job_title": "President",
                        "email": "jparrott@example.com",
                        "phone": "",
                        "linkedin_url": "",
                        "confidence": 0.95,
                    },
                ]
            }

    text = "Jason Parrott President jparrott@example.com Project Manager"
    people = team_contacts.extract_llm(
        FakeLLM(),
        business={"name": "Acme", "domain": "example.com"},
        text=text,
        source_url="https://example.com/team",
    )
    names = {p["name"] for p in people}
    assert names == {"Jason Parrott"}
    assert "Project Manager" not in names
    assert "Dale Construction Corporation" not in names
    assert "Steve W." not in names


def test_waterfall_tier_order_aiark_is_second() -> None:
    assert waterfall.TIER_ORDER == [
        "apify",
        "aiark",
        "getleads",
        "leadmagic",
        "fullenrich",
    ]
    assert waterfall.tier_allowed("aiark", "aiark")
    assert waterfall.tier_allowed("aiark", "getleads")
    assert not waterfall.tier_allowed("getleads", "aiark")


def test_waterfall_aiark_runs_before_getleads(monkeypatch) -> None:
    gl = MagicMock()
    gl.enabled = True
    gl.calls = 0
    gl.hits = 0
    gl.find_email.return_value = None
    gl.find_people.return_value = [
        PersonHit(
            first_name="Gary",
            last_name="Lopez",
            title="Owner",
            source_tier="getleads",
        )
    ]

    ark = MagicMock(enabled=True, calls=0, hits=0)
    ark.find_people.return_value = [
        PersonHit(
            first_name="Alice",
            last_name="Baker",
            title="CEO",
            source_tier="ai_ark",
        )
    ]
    lm = MagicMock(enabled=True, calls=0, hits=0)
    lm.find_email.return_value = EmailHit(email="x@y.com", source_tier="leadmagic")
    lm.find_people.return_value = []
    fe = MagicMock(enabled=True, calls=0, hits=0)
    fe.find_email_bulk.return_value = []
    fe.find_email.return_value = None

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
        [{"domain": "acme.test", "company_name": "Acme"}],
        need="dm",
        store=None,
        write_supabase=True,
        max_tier="leadmagic",
        run_apify=False,
    )
    assert out["dms_found"] == 1
    ark.find_people.assert_called()
    gl.find_people.assert_not_called()
    lm.find_people.assert_not_called()


def test_waterfall_max_tier_aiark_blocks_later(monkeypatch) -> None:
    gl = MagicMock()
    gl.enabled = True
    gl.calls = 0
    gl.hits = 0
    gl.find_email.return_value = EmailHit(email="g@acme.test", source_tier="getleads")
    gl.find_people.return_value = []

    ark = MagicMock(enabled=True, calls=0, hits=0)
    ark.find_people.return_value = []
    lm = MagicMock(enabled=True, calls=0, hits=0)
    lm.find_email.return_value = EmailHit(email="x@y.com", source_tier="leadmagic")
    lm.find_people.return_value = []
    fe = MagicMock(enabled=True, calls=0, hits=0)
    fe.find_email_bulk.return_value = []
    fe.find_email.return_value = None

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
        [{"domain": "acme.test", "company_name": "Acme"}],
        need="both",
        store=None,
        write_supabase=True,
        max_tier="aiark",
        run_apify=False,
    )
    assert out["max_tier"] == "aiark"
    ark.find_people.assert_called()
    gl.find_people.assert_not_called()
    gl.find_email.assert_not_called()
    lm.find_email.assert_not_called()
    lm.find_people.assert_not_called()
    fe.find_email_bulk.assert_not_called()
    assert out["vendors_enabled"]["ai_ark"] is True
    assert out["vendors_enabled"]["getleads"] is False
    assert out["vendors_enabled"]["leadmagic"] is False
    assert out["vendors_enabled"]["fullenrich"] is False


def test_looks_like_person_rejects_acceptance_cases() -> None:
    assert not team_contacts.looks_like_person("Project", "Manager")
    assert not team_contacts.looks_like_person("Dale Construction", "Corporation")
    assert not team_contacts.looks_like_person("Steve", "W.")
    assert team_contacts.looks_like_person("Jason", "Parrott")
