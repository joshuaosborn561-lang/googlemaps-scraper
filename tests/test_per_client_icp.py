"""ICP membership is per client_tag — one classify cannot overwrite another."""

from __future__ import annotations

from pathlib import Path

import pytest

from gmscraper import classify, export
from gmscraper.store import MissingClientTag, Store


def _insert_biz(store: Store, place_id: str, name: str, category: str, city: str) -> None:
    store.conn.execute(
        """INSERT INTO businesses
           (place_id, name, city, state, domain, website, main_category, types)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            place_id,
            name,
            city,
            "NY" if city == "Brooklyn" else "TX",
            f"{place_id}.example",
            f"https://{place_id}.example",
            category,
            "[]",
        ),
    )
    store.conn.execute(
        "INSERT INTO sites (domain, status, text, n_chars) VALUES (?,?,?,?)",
        (f"{place_id}.example", "ok", f"{name} {category}", 20),
    )
    store.conn.commit()


def test_backfill_legacy_verdicts_to_basco(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _insert_biz(store, "d1", "Brooklyn Cadillac", "Cadillac dealer", "Brooklyn")
    with store.conn as c:
        c.execute(
            "INSERT INTO verdicts (place_id, in_icp, confidence, reason, model) "
            "VALUES ('d1', 1, 0.9, 'franchise service dept', 'old')"
        )
    n = store.backfill_legacy_verdicts()
    assert n == 1
    row = store.conn.execute(
        "SELECT client_tag, in_icp, reason FROM business_icp WHERE place_id='d1'"
    ).fetchone()
    assert row["client_tag"] == "basco"
    assert row["in_icp"] == 1
    assert store.backfill_legacy_verdicts() == 0  # idempotent


def test_classify_without_client_tag_errors(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")

    class Dummy:
        model = "dummy"

        def json_chat(self, *a, **k):
            return {"in_icp": True, "confidence": 0.9, "reason": "x"}

    with pytest.raises(MissingClientTag, match="client_tag is required"):
        classify.run(store, Dummy(), "dealerships")


def test_export_icp_only_without_client_tag_errors(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    with pytest.raises(MissingClientTag, match="icp_only"):
        export.fetch_leads(store, icp_only=True)


def test_leads_summary_unscoped_is_labelled_cross_client(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _insert_biz(store, "d1", "Brooklyn Cadillac", "Cadillac dealer", "Brooklyn")
    store.save_verdict("d1", True, 0.9, "franchise", "test", client_tag="basco")
    out = export.leads_summary(store, backfill_cities=False)
    assert out["scope"] == "cross-client"
    assert "in_icp" not in out
    assert "basco" in out["by_client"]
    assert out["by_client"]["basco"]["in_icp"] == 1


def test_client_classify_does_not_clobber_another(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _insert_biz(store, "d1", "Brooklyn Cadillac", "Cadillac dealer", "Brooklyn")
    _insert_biz(store, "p1", "DFW Property Mgmt", "Property management company", "Dallas")
    store.save_verdict("d1", True, 0.95, "franchise service", "test", client_tag="basco")

    class Dummy:
        model = "dummy"

        def json_chat(self, *a, **k):
            prompt = a[1] if len(a) > 1 else ""
            cat = ""
            for line in str(prompt).splitlines():
                if line.startswith("Google Maps category:"):
                    cat = line.split(":", 1)[1].strip().lower()
                    break
            match = "property management" in cat
            return {
                "in_icp": match,
                "confidence": 0.8,
                "reason": "peterson match" if match else "not a property manager",
            }

    before = export.leads_summary(store, client_tag="basco", backfill_cities=False)
    assert before["in_icp"] == 1
    assert "Cadillac dealer" in before["in_icp_by_main_category"]

    res = classify.run(
        store, Dummy(), "commercial property managers", client_tag="peterson",
        require_geo=False,
    )
    assert res["done"] >= 1
    assert res["client_tag"] == "peterson"

    after_basco = export.leads_summary(store, client_tag="basco", backfill_cities=False)
    assert after_basco["in_icp"] == 1
    assert after_basco["classified"] == 1
    assert after_basco["in_icp_by_main_category"] == {"Cadillac dealer": 1}

    pete = export.leads_summary(store, client_tag="peterson", backfill_cities=False)
    assert pete["scope"] == "client"
    assert pete["client_tag"] == "peterson"
    assert "Cadillac dealer" not in pete["in_icp_by_main_category"]
    assert pete["in_icp"] >= 1
    assert "Property management company" in pete["in_icp_by_main_category"]

    # Basco row must still be the original verdict, not Peterson's rewrite.
    basco_row = store.conn.execute(
        "SELECT in_icp, reason FROM business_icp "
        "WHERE place_id='d1' AND client_tag='basco'"
    ).fetchone()
    assert basco_row["in_icp"] == 1
    assert basco_row["reason"] == "franchise service"

    pete_on_dealer = store.conn.execute(
        "SELECT in_icp, reason FROM business_icp "
        "WHERE place_id='d1' AND client_tag='peterson'"
    ).fetchone()
    # Peterson may classify the dealer too (same shared businesses table),
    # but that is a *separate* row — Basco is untouched.
    if pete_on_dealer is not None:
        assert pete_on_dealer["reason"] != "franchise service"


def test_stats_has_no_global_in_icp(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _insert_biz(store, "d1", "Brooklyn Cadillac", "Cadillac dealer", "Brooklyn")
    store.save_verdict("d1", True, 0.9, "franchise", "test", client_tag="basco")
    stats = store.stats()
    assert "in_icp" not in stats
    assert stats["icp_by_client"]["basco"]["in_icp"] == 1


def test_owner_icp_only_requires_client_tag(tmp_path: Path) -> None:
    from gmscraper import owner

    store = Store(tmp_path / "t.db")

    class Dummy:
        model = "dummy"

        def json_chat(self, *a, **k):
            return {"owner_name": None, "owner_title": None, "confidence": 0}

    with pytest.raises(MissingClientTag):
        owner.run(store, Dummy(), icp_only=True)


def test_vasco_alias_is_basco(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _insert_biz(store, "d1", "Brooklyn Cadillac", "Cadillac dealer", "Brooklyn")
    store.save_verdict("d1", True, 0.9, "x", "test", client_tag="vasco")
    tags = [
        r[0]
        for r in store.conn.execute("SELECT DISTINCT client_tag FROM business_icp")
    ]
    assert tags == ["basco"]
