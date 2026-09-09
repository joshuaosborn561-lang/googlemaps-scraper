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


def test_domain_of_strips_www_and_path() -> None:
    from gmscraper.mapsdata import domain_of

    assert domain_of("https://www.Acme.com/about/team") == "acme.com"
    assert domain_of("http://www.acme.com") == "acme.com"
    assert domain_of("acme.com/foo") == "acme.com"


def test_resolve_one_row_calls_details_only_on_pass(monkeypatch) -> None:
    from gmscraper import source_binding as sb

    binding = sb.SourceBinding(
        project_id="x",
        schema="s",
        table="t",
        key_column="id",
        address_column="addr",
        domain_column="domain",
        resolved_column="resolved",
        confidence_column="confidence",
        supabase_url="http://x",
        supabase_key="k",
    )
    patches: list[dict] = []
    monkeypatch.setattr(
        resolve_places.sb, "patch_row", lambda b, key, patch: patches.append(patch)
    )

    client = MagicMock()
    client.request_count = 0
    # Strong address match
    client.search.return_value = [
        {
            "place_id": "ChIJtest",
            "name": "Acme Partners",
            "address": "3102 Maple Ave Ste 500, Dallas, TX 75201",
            "zip": "75201",
            "website": "",
            "phone": "",
            "latitude": 1.0,
            "longitude": 2.0,
        }
    ]
    client.place_details.return_value = {
        "place_id": "ChIJtest",
        "website": "https://www.acme.com/store/1",
        "phone": "+12145551212",
    }

    row = {"id": 1, "addr": "3102 Maple Ave Ste 500, Dallas, TX 75201"}
    out = resolve_places.resolve_one_row(
        client, binding, row, strategy="address", min_confidence=0.6
    )
    assert out["status"] == "resolved"
    assert out["domain"] == "acme.com"
    assert out["website"] == "https://www.acme.com/store/1"
    assert out["phone"] == "+12145551212"
    client.place_details.assert_called_once_with("ChIJtest")
    assert patches[-1]["website"] == "https://www.acme.com/store/1"
    assert patches[-1]["domain"] == "acme.com"
    assert patches[-1]["phone"] == "+12145551212"


def test_resolve_one_row_skips_details_on_low_confidence(monkeypatch) -> None:
    from gmscraper import source_binding as sb

    binding = sb.SourceBinding(
        project_id="x",
        schema="s",
        table="t",
        key_column="id",
        address_column="addr",
        domain_column="domain",
        resolved_column="resolved",
        confidence_column="confidence",
        supabase_url="http://x",
        supabase_key="k",
    )
    monkeypatch.setattr(resolve_places.sb, "patch_row", lambda *a, **k: None)

    client = MagicMock()
    # Suite mismatch → low confidence
    client.search.return_value = [
        {
            "place_id": "ChIJreject",
            "name": "Other Tenant",
            "address": "3102 Maple Ave Ste 200, Dallas, TX 75201",
            "zip": "75201",
            "website": "https://other.test",
            "phone": "1",
        }
    ]
    out = resolve_places.resolve_one_row(
        client,
        binding,
        {"id": 1, "addr": "3102 Maple Ave Ste 500, Dallas, TX 75201"},
        strategy="address",
        min_confidence=0.6,
    )
    assert out["status"] == "low_confidence"
    client.place_details.assert_not_called()


def _binding(**extra):
    from gmscraper import source_binding as sb

    kw = dict(
        project_id="azpapwtnrbzywlnxxecz",
        schema="client_peterson",
        table="gc_targets",
        key_column="contractor_name",
        name_column="clean_name",
        address_column="address",
        supabase_url="http://x",
        supabase_key="k",
    )
    kw.update(extra)
    return sb.SourceBinding(**kw)


def _stub_run(monkeypatch, binding, *, pending=2, fetch_rows=None, resolve=None):
    monkeypatch.setattr(resolve_places.sb, "resolve_binding", lambda **kw: binding)
    monkeypatch.setattr(resolve_places.sb, "validate_binding", lambda b: b)
    monkeypatch.setattr(
        resolve_places.sb, "ensure_writeback_columns", lambda b: {"columns_ensured": 0}
    )
    monkeypatch.setattr(
        resolve_places,
        "estimate",
        lambda b, limit=0, details_only=False: {
            "pending_rows": pending,
            "blocked": False,
            "estimated_overage_usd": 0,
            "block_reason": None,
        },
    )
    monkeypatch.setattr(resolve_places.settings, "require_rapidapi", lambda: None)
    monkeypatch.setattr(
        resolve_places, "MapsDataClient", lambda *a, **k: MagicMock(request_count=1)
    )
    monkeypatch.setattr(resolve_places.sb, "patch_row", lambda *a, **k: True)
    rows = fetch_rows if fetch_rows is not None else [
        {"contractor_name": "Acme LLC", "clean_name": "Acme", "address": "1 Main"},
        {"contractor_name": "Beta Co", "clean_name": "Beta", "address": "2 Main"},
    ]
    state = {"n": 0}

    def _fetch(_b, limit=0, offset=0, details_only=False, attempted_since=""):
        state["n"] += 1
        return list(rows) if state["n"] == 1 else []

    monkeypatch.setattr(resolve_places.sb, "fetch_pending", _fetch)
    if resolve is not None:
        monkeypatch.setattr(resolve_places, "resolve_one_row", resolve)
    return state


def test_estimate_blocks_over_20pct_remaining_quota(monkeypatch) -> None:
    from gmscraper import source_binding as sb

    binding = sb.SourceBinding(project_id="x", schema="s", table="t", key_column="id")
    monkeypatch.setattr(
        resolve_places.sb, "count_pending", lambda b, details_only=False: 200
    )

    class _Store:
        def __init__(self, *_a, **_k):
            pass

        def requests_this_cycle(self, _day=1) -> int:
            return 299_000  # remaining 1,000 on ultra; 20% = 200; 200 rows × 2 = 400

    monkeypatch.setattr("gmscraper.store.Store", _Store)
    est = resolve_places.estimate(binding, limit=0)
    assert est["requests"] == 400
    assert est["remaining_quota"] == 1_000
    assert est["max_requests_without_override"] == 200
    assert est["quota_share_blocked"] is True
    assert est["blocked"] is True
    assert est["block_reason"] == "exceeds_20pct_remaining_quota"


def test_override_quota_guard_starts(monkeypatch) -> None:
    binding = _binding()
    monkeypatch.setattr(resolve_places.sb, "resolve_binding", lambda **kw: binding)
    monkeypatch.setattr(resolve_places.sb, "validate_binding", lambda b: b)
    monkeypatch.setattr(
        resolve_places.sb, "ensure_writeback_columns", lambda b: {"columns_ensured": 0}
    )
    monkeypatch.setattr(
        resolve_places,
        "estimate",
        lambda b, limit=0, details_only=False: {
            "pending_rows": 0,
            "blocked": True,
            "block_reason": "exceeds_20pct_remaining_quota",
            "estimated_overage_usd": 0,
        },
    )
    monkeypatch.setattr(resolve_places.settings, "require_rapidapi", lambda: None)
    monkeypatch.setattr(resolve_places.sb, "fetch_pending", lambda *a, **k: [])
    blocked = resolve_places.run(
        schema="client_peterson",
        table="gc_targets",
        key_column="contractor_name",
        address_column="address",
    )
    assert blocked["started"] is False
    assert blocked["blocked"] is True
    started = resolve_places.run(
        schema="client_peterson",
        table="gc_targets",
        key_column="contractor_name",
        address_column="address",
        override_quota_guard=True,
    )
    assert started["started"] is True
    assert started.get("quota_guard_overridden") is True


def test_run_stops_on_repeat_keys(monkeypatch) -> None:
    binding = _binding()
    same = [
        {"contractor_name": f"Co{i}", "clean_name": f"C{i}", "address": f"{i} Main"}
        for i in range(25)
    ]
    monkeypatch.setattr(resolve_places.sb, "resolve_binding", lambda **kw: binding)
    monkeypatch.setattr(resolve_places.sb, "validate_binding", lambda b: b)
    monkeypatch.setattr(
        resolve_places.sb, "ensure_writeback_columns", lambda b: {"columns_ensured": 0}
    )
    monkeypatch.setattr(
        resolve_places,
        "estimate",
        lambda b, limit=0, details_only=False: {
            "pending_rows": 100,
            "blocked": False,
            "estimated_overage_usd": 0,
        },
    )
    monkeypatch.setattr(resolve_places.settings, "require_rapidapi", lambda: None)
    monkeypatch.setattr(
        resolve_places, "MapsDataClient", lambda *a, **k: MagicMock(request_count=1)
    )
    monkeypatch.setattr(resolve_places.sb, "patch_row", lambda *a, **k: True)
    monkeypatch.setattr(
        resolve_places,
        "resolve_one_row",
        lambda *a, **k: {"status": "no_match"},
    )
    monkeypatch.setattr(resolve_places.sb, "fetch_pending", lambda *a, **k: list(same))
    out = resolve_places.run(
        schema="client_peterson",
        table="gc_targets",
        key_column="contractor_name",
        address_column="address",
        workers=1,
    )
    assert out["stop_reason"] == "repeat_keys"
    assert out["rows"] == 25
    assert out["rows"] <= 100


def test_run_stops_when_requests_exceed_3x_pending(monkeypatch) -> None:
    binding = _binding()
    _stub_run(monkeypatch, binding, pending=1, fetch_rows=[
        {"contractor_name": "Acme LLC", "clean_name": "Acme", "address": "1 Main"},
    ])
    monkeypatch.setattr(
        resolve_places, "MapsDataClient", lambda *a, **k: MagicMock(request_count=5)
    )
    monkeypatch.setattr(
        resolve_places, "resolve_one_row", lambda *a, **k: {"status": "no_match"}
    )
    out = resolve_places.run(
        schema="client_peterson",
        table="gc_targets",
        key_column="contractor_name",
        address_column="address",
        workers=1,
    )
    assert out["requests"] > 3 * 1
    assert out["stop_reason"] == "requests_exceed_3x_pending"
    assert out["rows"] == 1
    assert out["request_cap"] == 3


def test_run_excludes_rows_attempted_this_run(monkeypatch) -> None:
    binding = _binding()
    seen: dict = {}

    def _fetch(_b, limit=0, offset=0, details_only=False, attempted_since=""):
        seen["attempted_since"] = attempted_since
        return []

    _stub_run(monkeypatch, binding, pending=3, fetch_rows=[])
    monkeypatch.setattr(resolve_places.sb, "fetch_pending", _fetch)
    out = resolve_places.run(
        schema="client_peterson",
        table="gc_targets",
        key_column="contractor_name",
        address_column="address",
        workers=1,
    )
    assert seen["attempted_since"]
    assert "T" in seen["attempted_since"]
    assert out["run_started_at"] == seen["attempted_since"]


def test_run_checks_cancel_between_rows(monkeypatch) -> None:
    binding = _binding()
    _stub_run(monkeypatch, binding, pending=2)
    n = {"calls": 0}

    def _resolve(*_a, **_k):
        n["calls"] += 1
        return {"status": "no_match"}

    monkeypatch.setattr(resolve_places, "resolve_one_row", _resolve)
    monkeypatch.setattr(
        resolve_places, "_cancel_requested", lambda *_a, **_k: n["calls"] >= 1
    )
    out = resolve_places.run(
        schema="client_peterson",
        table="gc_targets",
        key_column="contractor_name",
        address_column="address",
        workers=1,
    )
    assert n["calls"] == 1
    assert out.get("cancelled") or out.get("stop_reason") == "cancelled"


def test_estimate_counts_two_requests_per_row(monkeypatch) -> None:
    from gmscraper import source_binding as sb

    binding = sb.SourceBinding(
        project_id="x", schema="s", table="t", key_column="id"
    )
    monkeypatch.setattr(
        resolve_places.sb, "count_pending", lambda b, details_only=False: 10
    )
    est = resolve_places.estimate(binding, limit=0)
    assert est["pending_rows"] == 10
    assert est["requests"] == 20
    assert est["requests_per_row"] == 2
    est_d = resolve_places.estimate(binding, limit=0, details_only=True)
    assert est_d["requests"] == 10
    assert est_d["requests_per_row"] == 1


def test_patch_row_calls_pp_patch_row(monkeypatch) -> None:
    from gmscraper import source_binding as sb

    seen: dict = {}

    def fake_rpc(_binding, fn, args):
        seen["fn"] = fn
        seen["args"] = args
        return True

    monkeypatch.setattr(sb, "rpc", fake_rpc)
    binding = sb.SourceBinding(
        project_id="azpapwtnrbzywlnxxecz",
        schema="client_peterson",
        table="gc_targets",
        key_column="contractor_name",
        name_column="clean_name",
        address_column="address",
        supabase_url="http://x",
        supabase_key="k",
    )
    assert sb.patch_row(binding, "Acme LLC", {"resolved": True, "confidence": 0.8})
    assert seen["fn"] == "pp_patch_row"
    assert seen["args"]["p_schema"] == "client_peterson"
    assert seen["args"]["p_table"] == "gc_targets"
    assert seen["args"]["p_key_column"] == "contractor_name"
    assert seen["args"]["p_key_value"] == "Acme LLC"
    assert seen["args"]["p_patch"]["resolved"] is True


def test_run_surfaces_per_row_errors_in_progress(monkeypatch) -> None:
    from gmscraper import source_binding as sb

    binding = sb.SourceBinding(
        project_id="azpapwtnrbzywlnxxecz",
        schema="client_peterson",
        table="gc_targets",
        key_column="contractor_name",
        name_column="clean_name",
        address_column="address",
        supabase_url="http://x",
        supabase_key="k",
    )
    monkeypatch.setattr(resolve_places.sb, "resolve_binding", lambda **kw: binding)
    monkeypatch.setattr(resolve_places.sb, "validate_binding", lambda b: b)
    monkeypatch.setattr(
        resolve_places.sb, "ensure_writeback_columns", lambda b: {"columns_ensured": 0}
    )
    monkeypatch.setattr(
        resolve_places,
        "estimate",
        lambda b, limit=0, details_only=False: {
            "pending_rows": 2,
            "blocked": False,
            "estimated_overage_usd": 0,
        },
    )
    monkeypatch.setattr(resolve_places.settings, "require_rapidapi", lambda: None)
    monkeypatch.setattr(
        resolve_places, "MapsDataClient", lambda *a, **k: MagicMock(request_count=1)
    )
    batches = [
        [
            {"contractor_name": "Acme LLC", "clean_name": "Acme", "address": "1 Main"},
            {"contractor_name": "Beta Co", "clean_name": "Beta", "address": "2 Main"},
        ],
        [],
    ]

    def _fetch(_b, limit=0, offset=0, details_only=False, attempted_since=""):
        return batches.pop(0) if batches else []

    monkeypatch.setattr(resolve_places.sb, "fetch_pending", _fetch)

    def _boom(*_a, **_k):
        raise sb.BindingError(
            "Supabase POST rpc/pp_patch_row failed (404): Could not find the function"
        )

    monkeypatch.setattr(resolve_places, "resolve_one_row", _boom)
    ticks: list[dict] = []

    out = resolve_places.run(
        schema="client_peterson",
        table="gc_targets",
        key_column="contractor_name",
        name_column="clean_name",
        address_column="address",
        project_id="azpapwtnrbzywlnxxecz",
        workers=1,
        on_progress=lambda **p: ticks.append(p),
    )
    assert out["errors"] == 2
    assert out["resolved"] == 0
    assert out["last_error"]
    assert "pp_patch_row" in str(out["last_error"])
    assert len(out["error_samples"]) == 2
    assert out["error_samples"][0]["key"] in ("Acme LLC", "Beta Co")
    errored_ticks = [t for t in ticks if t.get("errors")]
    assert errored_ticks
    assert errored_ticks[0].get("last_error")
    assert errored_ticks[0].get("error_samples")


def test_estimate_only_skips_schema_writes(monkeypatch) -> None:
    from gmscraper import source_binding as sb

    binding = sb.SourceBinding(
        project_id="x",
        schema="s",
        table="t",
        key_column="id",
        address_column="addr",
        supabase_url="http://x",
        supabase_key="k",
    )
    monkeypatch.setattr(resolve_places.sb, "resolve_binding", lambda **kw: binding)
    monkeypatch.setattr(resolve_places.sb, "validate_binding", lambda b: b)
    called = {"ensure": 0}

    def _ensure(_b):
        called["ensure"] += 1
        return {"added": []}

    monkeypatch.setattr(resolve_places.sb, "ensure_writeback_columns", _ensure)
    monkeypatch.setattr(
        resolve_places,
        "estimate",
        lambda b, limit=0, details_only=False: {
            "pending_rows": 3,
            "blocked": False,
            "requests": 3,
        },
    )
    out = resolve_places.run(
        schema="s",
        table="t",
        key_column="id",
        address_column="addr",
        estimate_only=True,
    )
    assert called["ensure"] == 0
    assert out["estimate_only"] is True
    assert out["ensured"] == {}


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
    monkeypatch.setattr(waterfall.gc_sync, "upsert_companies", lambda rows, **kw: len(rows))
    monkeypatch.setattr(
        waterfall.gc_sync, "insert_contacts_ignore_conflict", lambda rows, **kw: len(rows)
    )
    monkeypatch.setattr(waterfall.gc_sync, "insert_contacts", lambda rows, **kw: len(rows))

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
    monkeypatch.setattr(waterfall.gc_sync, "upsert_companies", lambda rows, **kw: len(rows))
    monkeypatch.setattr(
        waterfall.gc_sync, "insert_contacts_ignore_conflict", lambda rows, **kw: len(rows)
    )
    monkeypatch.setattr(waterfall.gc_sync, "insert_contacts", lambda rows, **kw: len(rows))

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
