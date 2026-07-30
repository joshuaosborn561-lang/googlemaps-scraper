"""Offline tests -- no API key, no network, no Ollama."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gmscraper import export, zips  # noqa: E402
from gmscraper.mapsdata import (  # noqa: E402
    domain_of,
    extract_list,
    normalize,
    parse_address_parts,
)
from gmscraper.store import Store  # noqa: E402


# ----------------------------------------------------------- normalization


def test_extract_list_handles_envelope_shapes():
    assert extract_list({"data": [{"a": 1}]}) == [{"a": 1}]
    assert extract_list({"results": [{"a": 1}]}) == [{"a": 1}]
    assert extract_list([{"a": 1}]) == [{"a": 1}]
    assert extract_list({"data": {"results": [{"a": 1}]}}) == [{"a": 1}]
    assert extract_list({"status": "OK"}) == []


def test_extract_list_falls_back_to_longest_list():
    payload = {"meta": [{"x": 1}], "rows": [{"y": 1}, {"y": 2}]}
    assert len(extract_list(payload)) == 2


def test_normalize_maps_common_field_names():
    item = {
        "business_id": "abc123",
        "name": "Smith Funeral Home",
        "full_address": "1 Main St, Akron, OH 44301",
        "phone_number": "+1 330-555-0100",
        "website": "https://www.smithfh.com/",
        "rating": "4.8",
        "review_count": "132",
        "type": "Funeral home",
        "types": ["Funeral home", "Cremation service"],
        "latitude": 41.07,
        "longitude": -81.51,
    }
    r = normalize(item, "44301", "funeral home")
    assert r["place_id"] == "abc123"
    assert r["name"] == "Smith Funeral Home"
    assert r["phone"] == "+1 330-555-0100"
    assert r["domain"] == "smithfh.com"
    assert r["rating"] == 4.8
    assert r["reviews"] == 132
    assert r["types"] == ["Funeral home", "Cremation service"]
    assert r["source_zip"] == "44301"
    assert r["raw"] is item


def test_normalize_accepts_alternate_names_and_nesting():
    item = {
        "placeId": "xyz",
        "title": "Acme HVAC",
        "formatted_address": "9 Oak Ave",
        "userRatingsTotal": 12,          # unknown alias -> stays empty, no crash
        "geometry": {"lat": 40.0, "lng": -75.0},
        "categories": "HVAC contractor, Heating contractor",
    }
    r = normalize(item)
    assert r["place_id"] == "xyz"
    assert r["name"] == "Acme HVAC"
    assert r["address"] == "9 Oak Ave"
    assert r["latitude"] == 40.0 and r["longitude"] == -75.0
    assert r["types"] == ["HVAC contractor", "Heating contractor"]
    assert r["main_category"] == "HVAC contractor"


def test_normalize_synthesizes_id_when_provider_gives_none():
    a = normalize({"name": "Joe Plumbing", "address": "5 Elm", "phone": "555"})
    b = normalize({"name": "Joe Plumbing", "address": "5 Elm", "phone": "555"})
    assert a["place_id"].startswith("syn:")
    assert a["place_id"] == b["place_id"]  # stable within a run -> dedups


def test_parse_address_parts():
    assert parse_address_parts("12 River Rd, Agawam, MA 01001") == ("Agawam", "MA", "01001")
    assert parse_address_parts("9 Oak Ave, Akron, OH 44301-1234, USA") == ("Akron", "OH", "44301")
    # No comma before the city: fall back to state+zip only.
    assert parse_address_parts("100 Main St Akron OH 44301") == ("", "OH", "44301")
    assert parse_address_parts("somewhere unparseable") == ("", "", "")
    assert parse_address_parts("") == ("", "", "")


def test_normalize_backfills_city_state_zip_from_address():
    r = normalize(
        {"business_id": "x", "name": "N",
         "full_address": "12 River Rd, Agawam, MA 01001"},
        source_zip="01002",
    )
    assert (r["city"], r["state"], r["zip"]) == ("Agawam", "MA", "01001")


def test_normalize_prefers_provider_components_over_parsing():
    r = normalize({
        "business_id": "x", "name": "N",
        "full_address": "12 River Rd, Agawam, MA 01001",
        "city": "Feeding Hills", "state": "ma", "zipcode": "01030",
    })
    assert (r["city"], r["state"], r["zip"]) == ("Feeding Hills", "MA", "01030")


def test_normalize_falls_back_to_source_zip():
    r = normalize({"business_id": "x", "name": "N", "full_address": "No zip here"},
                  source_zip="44301")
    assert r["zip"] == "44301"


def test_domain_of_strips_www_and_rejects_aggregators():
    assert domain_of("https://www.Example.COM/about") == "example.com"
    assert domain_of("example.com") == "example.com"
    assert domain_of("https://facebook.com/somebiz") == ""
    assert domain_of("https://business-page.yelp.com/x") == ""
    assert domain_of("") == ""
    assert domain_of(None) == ""
    assert domain_of("not a url") == ""


# ------------------------------------------------------------------ store


def _biz(pid: str, **kw):
    base = {
        "place_id": pid, "name": f"Biz {pid}", "address": "1 St", "city": "Akron",
        "state": "OH", "zip": "44301", "phone": "555", "website": "https://x.com",
        "domain": "x.com", "rating": 4.5, "reviews": 10, "main_category": "gym",
        "types": ["gym"], "latitude": 1.0, "longitude": 2.0, "maps_url": "",
        "source_zip": "44301", "source_category": "gym", "raw": {"id": pid},
    }
    base.update(kw)
    return base


def test_store_dedups_across_zips(tmp_path):
    s = Store(tmp_path / "t.db")
    assert s.upsert_businesses([_biz("a"), _biz("b")]) == 2
    # same business surfaced again by a neighbouring zip
    assert s.upsert_businesses([_biz("a", source_zip="44302")]) == 0
    assert s.stats()["businesses"] == 2


def test_store_job_checkpointing(tmp_path):
    s = Store(tmp_path / "t.db")
    s.queue_jobs(["44301", "44302"], ["gym", "yoga studio"])
    assert len(s.pending_jobs()) == 4
    s.finish_job("44301", "gym", 20)
    assert len(s.pending_jobs()) == 3
    # re-queueing must not reset completed work
    s.queue_jobs(["44301", "44302"], ["gym", "yoga studio"])
    assert len(s.pending_jobs()) == 3
    s.finish_job("44302", "gym", 0, "boom")
    st = s.stats()
    assert st["jobs_done"] == 1 and st["jobs_error"] == 1


def test_store_sites_and_verdicts(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_businesses([_biz("a"), _biz("b")])  # both share domain x.com
    assert s.queue_sites() == 1                  # one fetch, not two
    assert s.pending_sites() == ["x.com"]
    s.save_site("x.com", "ok", "hello world", ["https://x.com/"])
    assert s.get_site_text("x.com") == "hello world"
    assert s.pending_sites() == []

    s.save_verdict("a", True, 0.9, "clearly a gym", "gemma4:12b")
    s.save_verdict("a", False, 0.2, "revised", "gemma4:12b")  # upsert
    assert s.stats()["classified"] == 1 and s.stats()["in_icp"] == 0
    s.save_owner("a", "Jane Doe", "Owner", "website", 0.8, "gemma4:12b")
    assert s.stats()["owners_found"] == 1


# ----------------------------------------------------------------- export


def test_export_filters_and_flattens(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_businesses([_biz("a"), _biz("b", state="TX")])
    s.save_verdict("a", True, 0.9, "yes", "m")
    s.save_verdict("b", False, 0.9, "no", "m")
    s.save_owner("a", "Jane Doe", "Owner", "website", 0.8, "m")

    out = tmp_path / "leads.csv"
    assert export.run(s, out, icp_only=True) == 1
    text = out.read_text()
    assert "Jane Doe" in text and "Biz b" not in text
    assert ",gym," in text  # types JSON flattened to a plain string

    assert export.run(s, out, icp_only=False) == 2
    assert export.run(s, out, icp_only=False, states=["TX"]) == 1
    assert export.run(s, out, icp_only=True, with_owner=True) == 1
    assert export.run(s, out, icp_only=False, min_confidence=0.95) == 0


# ------------------------------------------------------------------- zips


def test_zip_build_is_offline_and_filtered(tmp_path):
    out = tmp_path / "z.csv"
    n = zips.build(out, types=["STANDARD"])
    assert n > 29_000                       # ~29.8k active STANDARD zips
    rows = zips.load(out)
    assert len(rows) == n
    assert all(len(r["zip"]) == 5 for r in rows)
    assert all(r["lat"] and r["lng"] for r in rows)
    assert "PR" not in {r["state"] for r in rows}   # territories excluded
    assert len(zips.load(out, states=["OH"])) < n
    assert len(zips.load(out, limit=10)) == 10


def test_zip_build_all_types_is_larger(tmp_path):
    small = zips.build(tmp_path / "a.csv", types=["STANDARD"])
    big = zips.build(tmp_path / "b.csv", types=["all"], include_territories=True)
    assert big > small > 0


# --------------------------------------------------------------- raw round trip


def test_raw_json_survives_for_renormalization(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_businesses([_biz("a", raw={"weird_key": "kept"})])
    row = next(s.iter_businesses())
    assert json.loads(row["raw_json"])["weird_key"] == "kept"
