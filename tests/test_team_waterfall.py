"""Team-page crawl and local contacts extraction."""

from __future__ import annotations

from pathlib import Path

from gmscraper import enrich_site, team_contacts
from gmscraper.store import Store


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
            "('acme.test','ok','[page_type=team url=https://acme.test/team]\n"
            "Jane Smith, Owner\n','[]')"
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
