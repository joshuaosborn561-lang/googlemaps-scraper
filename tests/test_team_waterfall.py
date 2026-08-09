"""Team-page crawl, contacts extraction, and enrichment waterfall."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from gmscraper import enrich_site, team_contacts, waterfall
from gmscraper.store import Store
from gmscraper.vendors.base import EmailHit, PersonHit
from gmscraper.vendors.fullenrich import _work_email_from_contact


def test_classify_page_type() -> None:
    assert enrich_site.classify_page_type("https://x.com/") == "home"
    assert enrich_site.classify_page_type("https://x.com/team") == "team"
    assert enrich_site.classify_page_type("https://x.com/our-team/") == "team"
    assert enrich_site.classify_page_type("https://x.com/leadership") == "team"
    assert enrich_site.classify_page_type("https://x.com/about-us") == "about"
    assert enrich_site.classify_page_type("https://x.com/who-we-are") == "about"


def test_extract_heuristic_name_title() -> None:
    text = """
    Meet Our Team
    Jane Smith, President
    Bob Jones - Founder
    Alice Wonder
    CEO
    """
    people = team_contacts.extract_heuristic(text, source_url="https://x.com/team")
    names = {p["name"] for p in people}
    assert "Jane Smith" in names
    assert "Bob Jones" in names
    assert all(p["source"] == "team_page" for p in people)


def test_site_pages_and_contacts(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    with store.conn as c:
        c.execute(
            """INSERT INTO businesses (place_id, name, domain, city, state)
               VALUES ('p1','Acme','acme.test','Dallas','TX')"""
        )
        c.execute(
            "INSERT INTO sites (domain, status, text, pages) VALUES "
            "('acme.test','ok','[page_type=team url=https://acme.test/team]\\n"
            "Jane Smith, Owner\\n','[]')"
        )
    n = store.save_site_pages(
        "acme.test",
        [
            {
                "url": "https://acme.test/team",
                "page_type": "team",
                "text": "Jane Smith, Owner\nBob Jones, President",
            }
        ],
    )
    assert n == 1
    pages = store.get_site_pages("acme.test", page_types=["team"])
    assert len(pages) == 1
    res = team_contacts.run(store, domains=["acme.test"], workers=1)
    assert res["contacts"] >= 1
    contacts = store.contacts_for_domain("acme.test")
    assert any(c["name"] == "Jane Smith" for c in contacts)
    assert store.stats()["contacts_found"] >= 1


def test_fullenrich_work_email_parse() -> None:
    email = _work_email_from_contact(
        {
            "contact_info": {
                "work_emails": [{"email": "jane@acme.test", "status": "DELIVERABLE"}]
            },
            "custom": {"idx": "0"},
        }
    )
    assert email == "jane@acme.test"


def test_waterfall_stops_at_getleads(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    gl = MagicMock()
    gl.enabled = True
    gl.calls = 0
    gl.hits = 0
    gl.find_email.return_value = EmailHit(
        email="jane@acme.test", source_tier="getleads", status="valid"
    )
    gl.find_people.return_value = [
        PersonHit(
            first_name="Jane",
            last_name="Smith",
            full_name="Jane Smith",
            title="Owner",
            source_tier="getleads",
        )
    ]

    lm = MagicMock()
    lm.enabled = True
    lm.calls = 0
    lm.hits = 0
    lm.find_email.return_value = EmailHit(
        email="should-not-call@x.com", source_tier="leadmagic"
    )

    fe = MagicMock()
    fe.enabled = True
    fe.calls = 0
    fe.hits = 0
    fe.find_email_bulk.return_value = []

    ark = MagicMock()
    ark.enabled = True
    ark.calls = 0
    ark.hits = 0

    wf = waterfall.Waterfall(
        getleads=gl, ai_ark=ark, leadmagic=lm, fullenrich=fe, store=store
    )
    # Patch module-level enrich to use our wf via direct call path
    hit = wf.resolve_email(
        {
            "first_name": "Jane",
            "last_name": "Smith",
            "domain": "acme.test",
            "company_name": "Acme",
            "email": "",
        }
    )
    assert hit and hit.email == "jane@acme.test"
    assert hit.source_tier == "getleads"
    lm.find_email.assert_not_called()
    fe.find_email_bulk.assert_not_called()


def test_waterfall_writes_counts_only(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "t.db")
    gl = MagicMock()
    gl.enabled = True
    gl.calls = 1
    gl.hits = 1
    gl.find_email.return_value = EmailHit(
        email="a@b.com", source_tier="getleads", status="valid"
    )
    gl.find_people.return_value = []

    monkeypatch.setattr(
        waterfall, "GetLeadsClient", lambda: gl
    )
    monkeypatch.setattr(
        waterfall, "AiArkClient", lambda: MagicMock(enabled=False, calls=0, hits=0)
    )
    monkeypatch.setattr(
        waterfall, "LeadMagicClient", lambda: MagicMock(enabled=False, calls=0, hits=0)
    )
    monkeypatch.setattr(
        waterfall, "FullEnrichClient", lambda: MagicMock(enabled=False, calls=0, hits=0)
    )

    upserted = {}

    def fake_companies(rows):
        upserted["companies"] = rows
        return len(rows)

    def fake_contacts(rows):
        upserted["contacts"] = rows
        return len(rows)

    monkeypatch.setattr(waterfall.gc_sync, "upsert_companies", fake_companies)
    monkeypatch.setattr(waterfall.gc_sync, "insert_contacts", fake_contacts)
    monkeypatch.setattr(
        waterfall.gc_sync, "insert_contacts_ignore_conflict", fake_contacts
    )

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
        run_apify=False,
    )
    assert out["rows_in"] == 1
    assert out["emails_found"] == 1
    assert out["companies_upserted"] == 1
    assert "csv" not in out and "items" not in out
    assert upserted["companies"][0]["email_source_tier"] == "getleads"
