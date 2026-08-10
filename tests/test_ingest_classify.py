"""ingest_external_leads, email chooser, scoped classify, resolve estimate."""

from __future__ import annotations

import json
from pathlib import Path

from gmscraper import classify, emails as email_lib, export, ingest
from gmscraper.store import Store


def test_choose_primary_prefers_website_domain() -> None:
    primary, ranked = email_lib.choose_primary(
        [
            "jcbeach23@yhaoo.com, parsonsplumbing.dfw@gmail.comp, "
            "info@parsonsplumbing.com, bob@gmail.com"
        ],
        website="https://www.parsonsplumbing.com",
        prefer="jcbeach23@yhaoo.com",
    )
    assert primary == "info@parsonsplumbing.com"
    assert "jcbeach23@yhaoo.com" in ranked
    assert ranked.index(primary) < ranked.index("jcbeach23@yhaoo.com")


def test_ingest_shovels_mapping(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    rows = [
        {
            "id": "shv-1",
            "business_name": "Parsons Plumbing",
            "name": "Jim Parsons",
            "website": "https://www.parsonsplumbing.com",
            "primary_email": "jcbeach23@yhaoo.com",
            "email": "info@parsonsplumbing.com, bob@gmail.com, jcbeach23@yhaoo.com",
            "address_city": "Dallas",
            "address_state": "TX",
            "primary_phone": "2145551212",
            "permit_count": 12,
        },
        {
            "id": "shv-2",
            "business_name": "No Site LLC",
            "name": "Jane Doe",
            "website": "",
            "primary_email": "jane@gmail.com",
            "email": "jane@gmail.com, office@nosite.example",
            "address_city": "Fort Worth",
            "address_state": "TX",
            "permit_count": 3,
        },
    ]
    res = ingest.run(store, rows, source_tag="shovels", dedupe_on="domain")
    assert res["inserted"] == 2
    assert res["source"] == "shovels"
    stats = store.stats()
    assert stats["businesses_by_source"]["shovels"] == 2

    b1 = store.get_business("shovels:shv-1")
    assert b1["name"] == "Parsons Plumbing"
    assert b1["domain"] == "parsonsplumbing.com"
    assert b1["city"] == "Dallas"
    assert b1["state"] == "TX"
    assert b1["permit_count"] == 12
    assert b1["source"] == "shovels"

    owner = store.conn.execute(
        "SELECT owner_name FROM owners WHERE place_id=?", ("shovels:shv-1",)
    ).fetchone()
    assert owner["owner_name"] == "Jim Parsons"

    # Export should pick website-domain email as primary and keep full list.
    leads = export.fetch_leads(store, icp_only=False, with_email=True)
    by_id = {r["place_id"]: r for r in leads}
    assert by_id["shovels:shv-1"]["email"] == "info@parsonsplumbing.com"
    assert "jcbeach23@yhaoo.com" in by_id["shovels:shv-1"]["all_emails"]

    # No-site row still keeps emails under ext: bucket.
    assert store.emails_for_business("shovels:shv-2", "") == [
        "jane@gmail.com",
        "office@nosite.example",
    ] or set(store.emails_for_business("shovels:shv-2", "")) == {
        "jane@gmail.com",
        "office@nosite.example",
    }


def test_ingest_dedupe_domain(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    row = {
        "id": "1",
        "business_name": "Acme",
        "website": "https://acme.example",
        "email": "info@acme.example",
        "address_city": "Dallas",
        "address_state": "TX",
    }
    assert ingest.run(store, [row], source_tag="shovels")["inserted"] == 1
    again = ingest.run(store, [{**row, "id": "2"}], source_tag="shovels", dedupe_on="domain")
    assert again["inserted"] == 0
    assert again["skipped_reasons"]["duplicate_domain"] == 1


def test_classify_source_and_force(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.insert_business(
        {
            "place_id": "shovels:1",
            "name": "GC One",
            "city": "Dallas",
            "state": "TX",
            "domain": "gcone.example",
            "website": "https://gcone.example",
            "source": "shovels",
            "types": [],
        }
    )
    store.insert_business(
        {
            "place_id": "maps:1",
            "name": "Maps Biz",
            "city": "Dallas",
            "state": "TX",
            "domain": "maps.example",
            "website": "https://maps.example",
            "source": "maps",
            "types": [],
        }
    )
    with store.conn as c:
        c.execute(
            "INSERT INTO sites (domain, status, text, pages, n_chars) VALUES "
            "('gcone.example','ok','commercial general contractor','[]',30),"
            "('maps.example','ok','commercial general contractor','[]',30)"
        )
    store.save_verdict("shovels:1", True, 0.9, "already", "test")

    class Dummy:
        model = "dummy"

        def json_chat(self, *a, **k):
            return {"in_icp": True, "confidence": 0.8, "reason": "gc"}

    # Without force, source=shovels has nothing pending.
    res = classify.run(store, Dummy(), "commercial GC", source="shovels")
    assert res["done"] == 0
    assert "already" in res["reason"] or "verdicts" in res["reason"]

    # force re-classifies only shovels.
    res2 = classify.run(
        store, Dummy(), "commercial GC", source="shovels", force=True, limit=10
    )
    assert res2["done"] == 1

    # maps row still unclassified until scoped to maps/all.
    pending_maps = store.conn.execute(
        "SELECT COUNT(*) FROM businesses b WHERE b.source='maps' "
        "AND b.place_id NOT IN (SELECT place_id FROM verdicts)"
    ).fetchone()[0]
    assert pending_maps == 1


def test_resolve_estimate_counts(tmp_path: Path) -> None:
    from gmscraper import resolve_domains

    store = Store(tmp_path / "t.db")
    store.insert_business(
        {
            "place_id": "shovels:a",
            "name": "Needs Site",
            "city": "Dallas",
            "state": "TX",
            "source": "shovels",
            "types": [],
        }
    )
    store.insert_business(
        {
            "place_id": "shovels:b",
            "name": "Has Site",
            "city": "Dallas",
            "state": "TX",
            "domain": "has.example",
            "website": "https://has.example",
            "source": "shovels",
            "types": [],
        }
    )
    est = resolve_domains.estimate(store, source="shovels")
    assert est["businesses_needing_domain"] == 1
    assert est["requests"] == 1
