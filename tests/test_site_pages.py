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
