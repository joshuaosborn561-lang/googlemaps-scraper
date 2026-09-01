"""export_csv / query_leads / leads_summary transport + clean filters."""

from __future__ import annotations

from pathlib import Path

from gmscraper import emails, export
from gmscraper.store import Store


def _seed(store: Store) -> None:
    with store.conn as c:
        c.execute(
            """INSERT INTO businesses
               (place_id, name, city, state, domain, website, main_category, types,
                latitude, longitude, source_zip, source_category, address, phone)
               VALUES
               ('p1','Acme GC','Dallas','TX','acmebuilt.com','https://acmebuilt.com',
                'general contractor','[]',32.7,-96.8,'75001','general contractor',
                '100 Main St, Dallas, TX 75001','2145550100'),
               ('p2','Blank City GC','','TX','blankcity.com','https://blankcity.com',
                'general contractor','[]',32.8,-96.9,'75002','general contractor',
                '200 Oak Ave, Plano, TX 75002','2145550200'),
               ('p3','Wix Shop','Frisco','TX','realbiz.com','https://realbiz.com',
                'general contractor','[]',33.1,-96.8,'75034','general contractor',
                '1 Elm, Frisco, TX 75034','2145550300'),
               ('p4','Example Mail Co','Irving','TX','examplemail.com','https://examplemail.com',
                'general contractor','[]',32.8,-96.9,'75038','general contractor',
                '9 Fake, Irving, TX 75038','2145550400'),
               ('p5','No Email GC','Dallas','TX','noemail.com','https://noemail.com',
                'general contractor','[]',32.7,-96.8,'75001','general contractor',
                '50 Main, Dallas, TX 75001','')"""
        )
        c.execute(
            "INSERT INTO emails (domain, email, source) VALUES "
            "('acmebuilt.com','info@acmebuilt.com','website'),"
            "('blankcity.com','hello@blankcity.com','website'),"
            "('realbiz.com','owner@realbiz.com','website'),"
            "('realbiz.com','support@wixpress.com','website'),"
            "('examplemail.com','example@mysite.com','website'),"
            "('examplemail.com','info@example.com','website')"
        )
        c.execute(
            """INSERT INTO verdicts (place_id, in_icp, confidence, reason, model)
               VALUES
               ('p1', 1, 0.9, 'commercial GC', 'test'),
               ('p2', 1, 0.85, 'commercial GC plano', 'test'),
               ('p3', 1, 0.8, 'has wix junk too', 'test'),
               ('p4', 1, 0.7, 'placeholder only', 'test'),
               ('p5', 1, 0.75, 'no email', 'test')"""
        )
    store.backfill_legacy_verdicts()


def test_placeholder_and_agency_helpers() -> None:
    assert emails.is_placeholder_or_agency("example@mysite.com")
    assert emails.is_placeholder_or_agency("info@example.com")
    assert emails.is_placeholder_or_agency("x@wixpress.com", "realbiz.com")
    assert emails.is_placeholder_or_agency("a@foo.wixpress.com", "realbiz.com")
    assert not emails.is_clean_lead_email("support@wixpress.com", "realbiz.com")
    assert emails.is_clean_lead_email("owner@realbiz.com", "realbiz.com")


def test_backfill_blank_cities(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _seed(store)
    n = export.backfill_blank_cities(store)
    assert n == 1
    city = store.conn.execute(
        "SELECT city FROM businesses WHERE place_id='p2'"
    ).fetchone()[0]
    assert city == "Plano"


def test_backfill_city_from_source_zip(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    with store.conn as c:
        c.execute(
            """INSERT INTO businesses
               (place_id, name, city, state, domain, address, source_zip, zip)
               VALUES ('pz','No Addr GC','','','x.com','','75016','')"""
        )
    n = export.backfill_blank_cities(store)
    assert n == 1
    row = store.conn.execute(
        "SELECT city, state, zip FROM businesses WHERE place_id='pz'"
    ).fetchone()
    assert row["city"] == "Irving"
    assert row["state"] == "TX"
    assert row["zip"] == "75016"


def test_export_payload_returns_csv_text(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _seed(store)
    payload = export.export_payload(
        store,
        icp_only=True,
        with_email=False,
        clean=True,
        client_tag="basco",
        include_reason=False,
        backfill_cities=True,
    )
    assert payload["capped_at"] == 5000
    assert "csv" in payload and payload["csv"].startswith("place_id,")
    assert "icp_reason" not in payload["columns"]
    # Placeholder-only row dropped; wix salvage keeps realbiz via owner@
    names = [
        line.split(",")[1]
        for line in payload["csv"].strip().splitlines()[1:]
        if line
    ]
    assert "Acme GC" in names
    assert "Blank City GC" in names or "Blank City GC" in payload["csv"]
    assert "Example Mail Co" not in payload["csv"]
    assert "Wix Shop" in payload["csv"]
    assert "owner@realbiz.com" in payload["csv"]
    assert "example@mysite.com" not in payload["csv"]
    # City backfilled
    assert "Plano" in payload["csv"]
    assert payload["cities_backfilled"] >= 1


def test_export_include_reason_opt_in(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _seed(store)
    payload = export.export_payload(
        store, icp_only=True, with_email=True, include_reason=True, clean=True,
        client_tag="basco",
    )
    assert "icp_reason" in payload["columns"]
    assert "commercial GC" in payload["csv"]


def test_query_leads_pagination(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _seed(store)
    page1 = export.query_leads(
        store, icp_only=True, page=1, page_size=2, clean=True, with_email=False,
        client_tag="basco",
    )
    assert page1["page"] == 1
    assert page1["page_size"] == 2
    assert page1["total"] >= 3
    assert len(page1["items"]) == 2
    assert page1["total_pages"] >= 2
    page2 = export.query_leads(
        store, icp_only=True, page=2, page_size=2, clean=True, with_email=False,
        client_tag="basco",
    )
    ids1 = {r["place_id"] for r in page1["items"]}
    ids2 = {r["place_id"] for r in page2["items"]}
    assert ids1.isdisjoint(ids2)


def test_query_page_size_capped(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _seed(store)
    out = export.query_leads(store, page_size=999, icp_only=True, client_tag="basco")
    assert out["page_size"] == 50


def test_leads_summary_counts(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _seed(store)
    summary = export.leads_summary(
        store, clean=True, backfill_cities=True, client_tag="basco"
    )
    assert summary["total_businesses"] == 5
    assert summary["in_icp"] == 5
    assert summary["classified"] == 5
    assert summary["unclassified"] == 0
    assert summary["with_website"] == 5
    assert summary["unique_domains"] == 5
    assert "Dallas" in summary["in_icp_by_city"] or "Plano" in summary["in_icp_by_city"]
    assert "general contractor" in summary["in_icp_by_main_category"]
    # clean email count excludes placeholder-only biz
    assert summary["with_email"] >= 2
    assert summary["with_email"] < 5


def test_clean_false_keeps_placeholders(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _seed(store)
    dirty = export.export_payload(
        store, icp_only=True, with_email=False, clean=False, backfill_cities=False,
        client_tag="basco",
    )
    assert "example@mysite.com" in dirty["csv"] or "Example Mail Co" in dirty["csv"]
