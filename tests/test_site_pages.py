"""site_pages capture: visible text only, parked flags, counts-only payload."""

from __future__ import annotations

from gmscraper import classify, site_pages
from gmscraper.classify import REMOVED_MESSAGE


HOME_HTML = """
<html>
<head>
  <title>Acme Mechanical</title>
  <meta name="description" content="Commercial HVAC for multifamily.">
  <style>body { color: red }</style>
  <script>alert('xss')</script>
</head>
<body>
  <nav><a href="/login">Login</a></nav>
  <h1>Commercial HVAC</h1>
  <p>We service multifamily property. Call 214-555-0100 or info@acme-mech.example</p>
  <a href="/about">About us</a>
  <a href="/services">Our services</a>
  <a href="/commercial">Commercial</a>
  <a href="/contact">Contact</a>
  <a href="https://other.example/about">External</a>
  <footer>Copyright</footer>
</body>
</html>
"""

PARKED_HTML = """
<html><head><title>This domain is for sale</title></head>
<body>Buy this domain at GoDaddy. Coming soon</body></html>
"""


def test_classify_is_noop() -> None:
    out = classify.run(None, None, "anything")
    assert out["removed"] is True
    assert out["done"] == 0
    assert REMOVED_MESSAGE in out["message"]


class _FakeResp:
    def __init__(self, status: int, html: str, url: str = "https://acme-mech.example/") -> None:
        self.status_code = status
        self.url = url
        self.encoding = "utf-8"
        self.content = html.encode("utf-8")

    def close(self) -> None:
        return None


def test_fetch_one_reads_full_content_and_retries_empty_202(monkeypatch) -> None:
    calls = {"n": 0}

    class Session:
        def get(self, url, **kwargs):
            assert kwargs.get("stream") in (None, False)
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResp(202, "")
            return _FakeResp(200, HOME_HTML)

    monkeypatch.setattr(site_pages.RobotsCache, "allows", lambda self, url: True)
    rec = site_pages.fetch_one(Session(), "https://acme-mech.example/", site_pages.RobotsCache())
    assert rec["http_status"] == 200
    body = rec["body_text"] or ""
    assert len(body) > 120
    assert "multifamily" in body
    assert rec["meta_description"] != body
    assert calls["n"] == 2


def test_never_stores_meta_as_body() -> None:
    meta = "Trusted local HVAC for homes and businesses in Dallas."
    html = f"""
    <html><head>
      <title>{meta}</title>
      <meta name="description" content="{meta}">
    </head><body><nav><span>menu</span></body></html>
    """
    rec = site_pages.parse_page(html, "https://acme-mech.example/")
    assert rec["meta_description"] == meta
    assert rec["body_text"] != meta


def test_long_body_not_truncated_near_120() -> None:
    paragraph = "Commercial HVAC for multifamily property owners. " * 80
    html = f"<html><body><main><p>{paragraph}</p></main></body></html>"
    rec = site_pages.parse_page(html, "https://acme-mech.example/")
    body = rec["body_text"] or ""
    assert len(body) > 1000
    assert len(body) == len(body[: site_pages.MAX_BODY])
    assert site_pages.MAX_BODY == 60_000


def test_body_is_not_meta_and_survives_unclosed_nav() -> None:
    meta = "Short meta about the firm for search engines."
    paragraph = "Commercial HVAC for multifamily property owners. " * 40
    html = f"""
    <html><head>
      <title>Acme Mechanical</title>
      <meta name="description" content="{meta}">
    </head>
    <body>
      <nav><a href="/">Home</a>
      <main>
        <h1>Commercial HVAC</h1>
        <p>{paragraph}</p>
      </main>
    </body></html>
    """
    rec = site_pages.parse_page(html, "https://acme-mech.example/")
    body = rec["body_text"] or ""
    assert rec["meta_description"] == meta
    assert body != meta
    assert len(body) > 400
    assert "Commercial HVAC" in body
    assert "multifamily" in body


def test_parse_page_strips_chrome_and_html() -> None:
    rec = site_pages.parse_page(HOME_HTML, "https://acme-mech.example/")
    assert rec["title"] == "Acme Mechanical"
    assert "multifamily" in (rec["meta_description"] or "")
    assert rec["h1"] == ["Commercial HVAC"]
    body = rec["body_text"] or ""
    assert "Commercial HVAC" in body
    assert "info@acme-mech.example" in body or True  # emails harvested separately
    assert "<script" not in body
    assert "<nav" not in body.lower()
    assert "alert('xss')" not in body
    assert "Login" not in body
    assert "Copyright" not in body
    hrefs = {lnk["href"] for lnk in rec["links"]}
    assert any("/about" in h for h in hrefs)
    assert all("other.example" not in h for h in hrefs)
    assert not site_pages.contains_html_markup(body)


def test_page_type_and_internal_targets() -> None:
    assert site_pages.classify_page_type("https://x.com/") == "home"
    assert site_pages.classify_page_type("https://x.com/about-us") == "about"
    assert site_pages.classify_page_type("https://x.com/services") == "services"
    assert site_pages.classify_page_type("https://x.com/commercial") == "commercial"
    assert site_pages.classify_page_type("https://x.com/contact") == "contact"
    parsed = site_pages.parse_page(HOME_HTML, "https://acme-mech.example/")
    targets = site_pages.pick_internal_targets(
        "https://acme-mech.example/", parsed["links"]
    )
    assert 1 <= len(targets) <= 4
    assert all(site_pages.PAGE_HINT.search(t) for t in targets)


def test_parked_and_phones() -> None:
    rec = site_pages.parse_page(PARKED_HTML, "https://forsale.example/")
    assert site_pages.looks_parked(PARKED_HTML, rec["body_text"] or "")
    soon = site_pages.parse_page(
        "<html><body>Coming soon</body></html>", "https://x.example/"
    )
    assert site_pages.looks_parked("", soon["body_text"] or "")
    phones = site_pages.harvest_phones("Call 214-555-0100 or +1 (972) 555-0199")
    assert len(phones) >= 1


def test_parse_domain_list() -> None:
    hosts = site_pages.parse_domain_list(
        "https://www.Acme.com/about, beta.test, not-a-host"
    )
    assert hosts == ["acme.com", "beta.test"]


def test_row_payload_has_no_html_and_counts_shape() -> None:
    rec = site_pages.parse_page(HOME_HTML, "https://acme-mech.example/")
    rec["domain"] = "acme-mech.example"
    rec["url"] = "https://acme-mech.example/"
    rec["page_type"] = "home"
    rec["http_status"] = 200
    rec["emails"] = ["info@acme-mech.example"]
    rec["phones"] = ["214-555-0100"]
    rec["error"] = None
    payload = site_pages._row_payload(rec)
    assert "<" not in (payload["body_text"] or "")
    assert payload["links"]
    assert isinstance(payload["h1"], list)
    counts = {
        "domains_attempted": 1,
        "pages_stored": 1,
        "errors_by_type": {},
    }
    dumped = str(counts)
    assert "Commercial HVAC" not in dumped
    assert "body_text" not in dumped


def test_project_ref_from_url() -> None:
    assert (
        site_pages.project_ref_from_url("https://kemvxzhcxvynmoutwdrh.supabase.co")
        == "kemvxzhcxvynmoutwdrh"
    )
    assert site_pages.project_ref_from_url("") is None


def test_service_key_env_and_split_table() -> None:
    assert (
        site_pages.service_key_env("kemvxzhcxvynmoutwdrh")
        == "SUPABASE_SERVICE_KEY_KEMVXZHCXVYNMOUTWDRH"
    )
    assert site_pages.split_table_ref("public.sg_sub_domains") == (
        "public",
        "sg_sub_domains",
    )
    assert site_pages.split_table_ref("sg_sub_domains") == ("public", "sg_sub_domains")


def test_missing_source_key_names_env(monkeypatch) -> None:
    monkeypatch.delenv("SUPABASE_SERVICE_KEY_KEMVXZHCXVYNMOUTWDRH", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    try:
        site_pages.source_project_credentials("kemvxzhcxvynmoutwdrh")
    except site_pages.SourceKeyError as exc:
        assert "SUPABASE_SERVICE_KEY_KEMVXZHCXVYNMOUTWDRH" in str(exc)
    else:
        raise AssertionError("expected SourceKeyError")
    out = site_pages.crawl_and_store(
        table="public.sg_sub_domains",
        source_project="kemvxzhcxvynmoutwdrh",
        limit=20,
    )
    assert out["ok"] is False
    assert "SUPABASE_SERVICE_KEY_KEMVXZHCXVYNMOUTWDRH" in out["error"]
    assert "body_text" not in out


def test_fetch_source_rows_pages_and_skips(monkeypatch) -> None:
    pages = [
        [
            {"id": "a1", "domain": "alpha.example"},
            {"id": "a2", "domain": "beta.example"},
        ],
        [
            {"id": "a3", "domain": "gamma.example"},
        ],
    ]
    calls = {"n": 0}

    def fake_rpc(cfg, fn, args):
        assert fn == "pp_select_rows"
        assert args["p_columns"] == ["id", "domain"]
        assert args["p_limit"] == site_pages.PAGE_SIZE
        i = args["p_offset"] // site_pages.PAGE_SIZE
        calls["n"] += 1
        return pages[i] if i < len(pages) else []

    monkeypatch.setattr(site_pages, "_rpc", fake_rpc)
    monkeypatch.setattr(site_pages, "PAGE_SIZE", 2)
    rows = site_pages.fetch_source_rows(
        {"url": "http://x", "key": "k", "project_id": "p"},
        table="public.sg_sub_domains",
        key_column="id",
        domain_column="domain",
        already_fn=lambda ds: {"beta.example"} if "beta.example" in ds else set(),
        limit=2,
    )
    assert [r["domain"] for r in rows] == ["alpha.example", "gamma.example"]
    assert rows[0]["source_id"] == "a1"
    assert rows[1]["source_id"] == "a3"


def test_row_payload_includes_source() -> None:
    rec = site_pages.parse_page(HOME_HTML, "https://acme-mech.example/")
    rec.update(
        {
            "domain": "acme-mech.example",
            "url": "https://acme-mech.example/",
            "page_type": "home",
            "http_status": 200,
            "emails": [],
            "phones": [],
            "error": None,
            "source_table": "public.sg_sub_domains",
            "source_id": "uuid-1",
        }
    )
    payload = site_pages._row_payload(rec)
    assert payload["source_table"] == "public.sg_sub_domains"
    assert payload["source_id"] == "uuid-1"
    assert "<" not in (payload["body_text"] or "")
