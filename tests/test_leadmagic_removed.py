"""LeadMagic was dropped Oct 8 2026 — no live tier, legacy names are no-ops."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gmscraper import gc_sync, owner, pipeline, waterfall
from gmscraper.store import Store
from gmscraper.vendors.base import EmailHit, PersonHit
from mcp_server import server

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_COLUMNS = ("dl_status", "sg_exclude")
FORBIDDEN_SKIP_COLUMNS = (
    "skip_email",
    "skip_phone",
    "skip_dm",
    "skip_enrich",
    "skip_owner",
    "skip_waterfall",
)


def _patch_vendors(monkeypatch, *, getleads=None, ai_ark=None, fullenrich=None):
    gl = getleads or MagicMock(enabled=True, calls=0, hits=0)
    if getleads is None:
        gl.find_email.return_value = None
        gl.find_people.return_value = []
    ark = ai_ark or MagicMock(enabled=False, calls=0, hits=0)
    fe = fullenrich or MagicMock(enabled=True, calls=0, hits=0)
    if fullenrich is None:
        fe.find_email.return_value = None
        fe.find_email_bulk.return_value = []
    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: gl)
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: ark)
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: fe)
    monkeypatch.setattr(waterfall.gc_sync, "upsert_companies", lambda rows, **kw: len(rows))
    monkeypatch.setattr(
        waterfall.gc_sync, "insert_contacts_ignore_conflict", lambda rows, **kw: len(rows)
    )
    monkeypatch.setattr(waterfall.gc_sync, "insert_contacts", lambda rows, **kw: len(rows))
    return gl, ark, fe


def test_tier_order_and_default_have_no_leadmagic() -> None:
    assert waterfall.TIER_ORDER == ["apify", "aiark", "getleads", "fullenrich"]
    assert waterfall.DEFAULT_MAX_TIER == "getleads"
    assert "leadmagic" not in waterfall.TIER_ORDER
    assert "leadmagic" not in waterfall.TIER_RANK
    assert not hasattr(waterfall, "LeadMagicClient")


def test_legacy_max_tier_names_are_noop_warnings() -> None:
    for raw in ("leadmagic", "lm", "lead_magic", "lead-magic", "LeadMagic"):
        assert waterfall.normalize_max_tier(raw) == "getleads"
        canon, deprecated, warnings = waterfall.resolve_max_tier(raw)
        assert canon == "getleads"
        assert deprecated is not None
        assert warnings
        assert "retired LeadMagic" in warnings[0]
        assert "getleads" in warnings[0]
        assert not waterfall.tier_allowed("fullenrich", raw)


def test_unknown_max_tier_still_errors() -> None:
    with pytest.raises(ValueError, match="max_tier must be one of"):
        waterfall.normalize_max_tier("not-a-tier")


def test_legacy_max_tier_does_not_call_fullenrich(monkeypatch) -> None:
    gl, _ark, fe = _patch_vendors(monkeypatch)
    gl.find_email.return_value = None

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
        store=None,
        write_supabase=True,
        max_tier="leadmagic",
        run_apify=False,
    )
    assert out["max_tier"] == "getleads"
    assert out["deprecated_max_tier"] == "leadmagic"
    assert any("retired LeadMagic" in w for w in out["warnings"])
    assert "leadmagic" not in out["vendors_enabled"]
    assert "leadmagic" not in out["tier_stats"]
    fe.find_email.assert_not_called()
    fe.find_email_bulk.assert_not_called()
    assert out["vendors_enabled"]["fullenrich"] is False


def test_waterfall_has_no_leadmagic_client() -> None:
    wf = waterfall.Waterfall(max_tier="lm")
    assert not hasattr(wf, "leadmagic")
    assert wf.max_tier == "getleads"


def test_resolve_email_skips_retired_tier() -> None:
    gl = MagicMock(enabled=True, calls=0, hits=0)
    gl.find_email.return_value = None
    fe = MagicMock(enabled=True, calls=0, hits=0)
    fe.find_email.return_value = EmailHit(
        email="x@y.com", source_tier="fullenrich", status="valid"
    )
    wf = waterfall.Waterfall(getleads=gl, fullenrich=fe, max_tier="leadmagic")
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


def test_resolve_dm_never_walks_leadmagic() -> None:
    ark = MagicMock(enabled=False, calls=0, hits=0)
    gl = MagicMock(enabled=True, calls=0, hits=0)
    gl.find_people.return_value = [
        PersonHit(first_name="Jane", last_name="Smith", title="Owner", source_tier="getleads")
    ]
    wf = waterfall.Waterfall(getleads=gl, ai_ark=ark, max_tier="lm")
    person = wf.resolve_dm({"domain": "acme.test", "company_name": "Acme", "full_name": ""})
    assert person and person.source_tier == "getleads"
    gl.find_people.assert_called_once()


def test_pipeline_run_accepts_legacy_max_tier(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(pipeline.sb, "resolve_binding", lambda **kw: None)
    out = pipeline.run(
        store,
        schema="public",
        table="does_not_matter",
        key_column="id",
        stages="contacts",
        max_tier="leadmagic",
    )
    assert out["max_tier"] == "getleads"
    assert out["deprecated_max_tier"] == "leadmagic"
    assert any("retired LeadMagic" in w for w in out["warnings"])
    assert out["per_stage"]["contacts"]["skipped"] is True


def test_find_owners_has_no_leadmagic_tier() -> None:
    assert "LeadMagicClient" not in inspect.getsource(owner)
    assert "vendors.leadmagic" not in inspect.getsource(owner)
    assert "LeadMagicClient" not in inspect.getsource(pipeline)
    src = inspect.getsource(server.find_owners)
    assert "use_paid_fallback" in src
    assert "LeadMagicClient" not in src
    assert "enrich_waterfall" not in src


def test_mcp_enrich_waterfall_default_is_getleads() -> None:
    wf_sig = inspect.signature(server.enrich_waterfall)
    assert wf_sig.parameters["max_tier"].default == "getleads"
    desc = inspect.getdoc(server.enrich_waterfall) or ""
    assert "leadmagic" in desc.lower()
    assert "no-op" in desc.lower()
    pipe_sig = inspect.signature(server.pipeline_run)
    assert pipe_sig.parameters["max_tier"].default == "getleads"


def test_no_leadmagic_vendor_module() -> None:
    assert not (ROOT / "gmscraper" / "vendors" / "leadmagic.py").exists()
    with pytest.raises(ImportError):
        __import__("gmscraper.vendors.leadmagic")


def test_enrich_writes_never_touch_forbidden_columns() -> None:
    company = gc_sync.company_row(domain="acme.test", company_name="Acme")
    contact = gc_sync.contact_row(
        domain="acme.test", first_name="Jane", last_name="Smith"
    )
    for row in (company, contact):
        for col in FORBIDDEN_COLUMNS + FORBIDDEN_SKIP_COLUMNS:
            assert col not in row
        assert not any(k.startswith("skip_") for k in row)
        assert "dl_status" not in row
        assert "sg_exclude" not in row


def test_source_does_not_write_forbidden_queue_columns() -> None:
    """dl_status / sg_exclude / skip_* are queue columns — do not read or write."""
    paths = [
        ROOT / "gmscraper" / "waterfall.py",
        ROOT / "gmscraper" / "pipeline.py",
        ROOT / "gmscraper" / "gc_sync.py",
        ROOT / "gmscraper" / "owner.py",
        ROOT / "gmscraper" / "supabase_sync.py",
        ROOT / "mcp_server" / "server.py",
    ]
    for path in paths:
        text = path.read_text()
        assert "dl_status" not in text, f"{path.name} mentions dl_status"
        assert "sg_exclude" not in text, f"{path.name} mentions sg_exclude"
        for col in FORBIDDEN_SKIP_COLUMNS:
            assert col not in text, f"{path.name} mentions {col}"
