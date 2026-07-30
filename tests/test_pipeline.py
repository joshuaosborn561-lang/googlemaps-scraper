"""Offline tests -- no API key, no network, no Ollama."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gmscraper import emails as email_lib  # noqa: E402
from gmscraper import export, zips  # noqa: E402
from gmscraper.brief import Plan  # noqa: E402
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


# ----------------------------------------------------------------- emails


def test_harvest_finds_mailto_text_and_obfuscated():
    html = """<html><body>
      <a href="mailto:info@riversidefh.com">Email us</a>
      <p>Owner: margaret@riversidefh.com</p>
      <p>Billing: billing [at] riversidefh [dot] com</p>
      <img src="logo@2x.png"><script>x="a3f9c1d2e4b5a6c7@cdn.io"</script>
    </body></html>"""
    got = email_lib.harvest(html)
    assert "info@riversidefh.com" in got
    assert "margaret@riversidefh.com" in got
    assert "billing@riversidefh.com" in got
    assert not any(e.endswith(".png") for e in got)


def test_harvest_rejects_boilerplate_and_junk():
    html = """<a href="mailto:noreply@x.com">x</a>
              <p>you@example.com sentry@wixpress.com webmaster@x.com</p>"""
    assert email_lib.harvest(html) == set()


def test_email_ranking_prefers_owner_then_own_domain():
    pool = ["info@fh.com", "margaret@fh.com", "someone@gmail.com", "careers@fh.com"]
    # Owner known -> their personal address wins outright.
    assert email_lib.best_for(pool, "fh.com", "Margaret A. Whitfield") == "margaret@fh.com"
    # Owner unknown -> a named human on the company domain still beats the role
    # inbox, which is what you want for cold outreach.
    assert email_lib.best_for(pool, "fh.com", "") == "margaret@fh.com"

    order = email_lib.rank(pool, "fh.com")
    assert order.index("info@fh.com") < order.index("careers@fh.com")   # role priority
    assert order.index("info@fh.com") < order.index("someone@gmail.com")  # own domain


def test_email_ranking_role_inbox_wins_when_no_human_present():
    pool = ["careers@fh.com", "info@fh.com", "owner@gmail.com"]
    assert email_lib.best_for(pool, "fh.com", "") == "info@fh.com"


def test_email_ranking_handles_empty():
    assert email_lib.best_for([], "fh.com", "X") == ""


def test_store_saves_and_groups_emails(tmp_path):
    s = Store(tmp_path / "t.db")
    assert s.save_emails("x.com", {"a@x.com", "b@x.com"}) == 2
    assert s.save_emails("x.com", {"a@x.com"}) == 0          # idempotent
    assert sorted(s.emails_by_domain()["x.com"]) == ["a@x.com", "b@x.com"]
    assert s.stats()["domains_with_email"] == 1


def test_export_includes_ranked_email(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_businesses([_biz("a", domain="fh.com", website="https://fh.com")])
    s.save_verdict("a", True, 0.9, "ok", "m")
    s.save_owner("a", "Margaret Whitfield", "Owner", "website", 0.9, "m")
    s.save_emails("fh.com", {"info@fh.com", "margaret@fh.com"})

    out = tmp_path / "l.csv"
    assert export.run(s, out) == 1
    body = out.read_text().splitlines()[1]
    assert "margaret@fh.com" in body          # chosen
    assert "info@fh.com" in body              # kept in all_emails

    # --with-email filters out businesses with no address on file
    s.upsert_businesses([_biz("b", domain="none.com")])
    s.save_verdict("b", True, 0.9, "ok", "m")
    assert export.run(s, out, with_email=True) == 1
    assert export.run(s, out, with_email=False) == 2


def test_export_quality_filters(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_businesses([_biz("a", rating=4.8, reviews=120),
                         _biz("b", rating=3.1, reviews=4)])
    for pid in ("a", "b"):
        s.save_verdict(pid, True, 0.9, "ok", "m")
    out = tmp_path / "l.csv"
    assert export.run(s, out, min_rating=4.0) == 1
    assert export.run(s, out, min_reviews=50) == 1
    assert export.run(s, out, min_rating=4.0, min_reviews=200) == 0


# ------------------------------------------------------------------- plan


def test_plan_from_model_cleans_up_output():
    p = Plan.from_model({
        "vertical": "HVAC Contractors!",
        "categories": ["HVAC contractor", "hvac contractor  ", "Heating Contractor", ""],
        "icp": "Independent  HVAC   shops.\nExclude wholesalers.",
        "states": ["oh", "MI", "ZZ", "OH"],          # dupe + invalid dropped
        "min_rating": "4.0", "min_reviews": "20",
        "require_website": True, "require_phone": False,
        "require_email": True, "require_owner": True,
    })
    assert p.vertical == "hvac_contractors"
    assert p.categories == ["hvac contractor", "heating contractor"]   # deduped
    assert p.states == ["OH", "MI"]
    assert p.min_rating == 4.0 and p.min_reviews == 20
    assert p.icp == "Independent HVAC shops. Exclude wholesalers."
    assert p.require_email and p.require_owner and not p.require_phone


def test_plan_tolerates_garbage_numbers():
    p = Plan.from_model({"vertical": "", "categories": ["gym"], "icp": "x",
                         "states": [], "min_rating": "n/a", "min_reviews": None})
    assert p.vertical == "custom" and p.min_rating == 0.0 and p.min_reviews == 0


def test_plan_yaml_block_is_valid_yaml():
    import yaml
    p = Plan(vertical="hvac", categories=["hvac contractor", "heating contractor"],
             icp="Independent HVAC shops that serve homeowners. " * 4)
    data = yaml.safe_load(p.to_yaml_block())
    assert data["hvac"]["categories"] == ["hvac contractor", "heating contractor"]
    assert "Independent HVAC shops" in data["hvac"]["icp"]


def test_plan_describe_reports_cost_against_the_subscription_tier():
    from gmscraper.config import PLANS

    p = Plan(vertical="hvac", categories=["a", "b"], icp="x", states=["OH"])
    # 2,000 requests fits inside Pro's 30,000/mo quota -> no extra charge.
    text = p.describe(1000, PLANS["pro"])
    assert "2,000" in text and "OH" in text and "fits inside" in text

    # Same run with the quota nearly gone is billed as overage.
    text = p.describe(1000, PLANS["pro"], used_this_month=29_500)
    assert "$1.50" in text and "1,500" in text


def test_plan_cost_model_matches_the_published_tiers():
    from gmscraper.config import PLANS

    # National funeral vertical: 29,673 zips x 12 categories.
    n = 29_673 * 12
    assert n == 356_076

    cost, billable = PLANS["ultra"].cost_for(n)
    assert billable == 56_076                       # 300,000 included
    assert round(cost, 2) == 50.47
    assert round(PLANS["ultra"].monthly_usd + cost, 2) == 75.47

    # Mega swallows it whole.
    assert PLANS["mega"].cost_for(n) == (0.0, 0)

    # Basic is a hard limit -- surfaced as infinite, not a small number.
    cost, billable = PLANS["basic"].cost_for(n)
    assert cost == float("inf") and billable == 355_076


def test_plan_cost_accounts_for_quota_already_spent():
    from gmscraper.config import PLANS

    pro = PLANS["pro"]
    assert pro.cost_for(10_000, already_used=0) == (0.0, 0)
    cost, billable = pro.cost_for(10_000, already_used=25_000)
    assert billable == 5_000 and round(cost, 2) == 5.00


# --------------------------------------------------------------- raw round trip


def test_raw_json_survives_for_renormalization(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_businesses([_biz("a", raw={"weird_key": "kept"})])
    row = next(s.iter_businesses())
    assert json.loads(row["raw_json"])["weird_key"] == "kept"


def test_cheapest_plan_matches_the_break_even_points():
    from gmscraper.config import PLANS, cheapest_plan

    # One category nationally (29,673 zips) fits inside pro's 30,000 quota.
    assert cheapest_plan(29_673)[0].name == "pro"
    # Pro and ultra tie at exactly 50,000 requests; pro wins below.
    assert cheapest_plan(49_000)[0].name == "pro"
    assert round(PLANS["pro"].monthly_usd + PLANS["pro"].cost_for(50_000)[0], 2) == 25.00
    # A national vertical belongs on ultra, not pro.
    plan, total = cheapest_plan(356_076)
    assert plan.name == "ultra" and round(total, 2) == 75.47
    # Mega takes over past ~550k requests.
    assert cheapest_plan(600_000)[0].name == "mega"
    # basic is never chosen -- it cannot serve anything past its hard limit.
    assert cheapest_plan(5_000)[0].name != "basic"


def test_cycle_start_follows_the_subscription_anniversary():
    from datetime import date
    from gmscraper.config import cycle_start

    # Subscribed on the 30th: on Aug 5 you are inside the cycle that began Jul 30.
    assert cycle_start(30, date(2026, 8, 5)) == "2026-07-28"   # clamped to 28
    assert cycle_start(15, date(2026, 8, 5)) == "2026-07-15"
    assert cycle_start(15, date(2026, 8, 20)) == "2026-08-15"
    # Default day 1 behaves like the calendar month.
    assert cycle_start(1, date(2026, 8, 20)) == "2026-08-01"
    # January rolls back into the previous year.
    assert cycle_start(15, date(2026, 1, 3)) == "2025-12-15"


def test_requests_this_cycle_counts_billed_jobs_only(tmp_path):
    s = Store(tmp_path / "t.db")
    s.queue_jobs(["1", "2", "3"], ["gym"])
    s.finish_job("1", "gym", 20)              # done  -> billed
    s.finish_job("2", "gym", 0, "boom")       # error -> still billed
    assert s.requests_since("1970-01-01") == 2
    assert s.requests_since("2999-01-01") == 0   # nothing in a future cycle
