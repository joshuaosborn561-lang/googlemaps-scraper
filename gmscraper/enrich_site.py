"""Stage 3: turn a business website into plain text with html2text.

Fetches the homepage, then a shallow same-domain crawl of about/team pages
(max 3, one level deep). Per-page text is stored tagged by page_type; the
combined blob on `sites.text` remains for classify/owner compatibility.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Sequence
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import html2text
import requests

from . import emails as email_lib
from .store import Store

UA = (
    "Mozilla/5.0 (compatible; gmscraper/1.0; local lead research; "
    "+https://github.com/joshuaosborn561-lang/googlemaps-scraper)"
)

# Explicit team/about paths the user asked for (plus close variants).
TEAM_ABOUT_PATH = re.compile(
    r"(?:^|/)(?:"
    r"about(?:-us)?|team|our-team|leadership|management|staff|people|who-we-are"
    r")(?:/|$)",
    re.I,
)
# Broader interesting links kept for email harvest / classify context.
INTERESTING = re.compile(
    r"(about|our-?story|our-?team|team|staff|leadership|management|meet|"
    r"who-?we-?are|contact|owner|founder|history|bio|people)",
    re.I,
)
HREF = re.compile(r'href=["\']([^"\'#]+)["\']', re.I)
WS = re.compile(r"\n{3,}")

MAX_TEAM_PAGES = 3          # about/team pages beyond homepage
MAX_CHARS = 12_000
MAX_BYTES = 2_000_000
MAX_PAGE_CHARS = 8_000


def _converter() -> html2text.HTML2Text:
    h = html2text.HTML2Text()
    h.ignore_links = True
    h.ignore_images = True
    h.ignore_emphasis = True
    h.body_width = 0
    h.skip_internal_links = True
    return h


def classify_page_type(url: str) -> str:
    """Tag a URL as home / team / about / other."""
    path = (urlsplit(url).path or "/").rstrip("/") or "/"
    if path == "/":
        return "home"
    low = path.lower()
    if re.search(
        r"(?:^|/)(team|our-team|leadership|management|staff|people)(?:/|$)", low
    ):
        return "team"
    if re.search(r"(?:^|/)(about(?:-us)?|who-we-are|our-story)(?:/|$)", low):
        return "about"
    if INTERESTING.search(low):
        return "other"
    return "other"


class RobotsCache:
    """Per-domain robots.txt, fetched once. Failure to fetch means allow."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._cache: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allows(self, url: str) -> bool:
        if not self.enabled:
            return True
        host = urlsplit(url).netloc
        with self._lock:
            rp = self._cache.get(host, ...)
        if rp is ...:
            rp = self._fetch(url)
            with self._lock:
                self._cache[host] = rp
        if rp is None:
            return True
        try:
            return rp.can_fetch(UA, url)
        except Exception:  # noqa: BLE001
            return True

    def _fetch(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        robots = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            r = requests.get(robots, headers={"User-Agent": UA}, timeout=10)
            if r.status_code != 200 or not r.text.strip():
                return None
            rp = RobotFileParser()
            rp.parse(r.text.splitlines())
            return rp
        except requests.RequestException:
            return None


def _get(session: requests.Session, url: str, timeout: int) -> str | None:
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True, stream=True)
        if r.status_code != 200:
            return None
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype.lower() and ctype:
            return None
        body = r.raw.read(MAX_BYTES, decode_content=True) or b""
        return body.decode(r.encoding or "utf-8", errors="replace")
    except (requests.RequestException, ValueError):
        return None
    finally:
        try:
            r.close()  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass


def _team_about_links(html: str, base: str) -> list[str]:
    """Same-domain about/team URLs, max MAX_TEAM_PAGES, one level deep."""
    host = urlsplit(base).netloc
    seen, preferred, other = set(), [], []
    for href in HREF.findall(html):
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base, href)
        if urlsplit(url).netloc != host:
            continue
        path = urlsplit(url).path or ""
        if not path or path == "/" or len(path) > 120:
            continue
        if url in seen:
            continue
        seen.add(url)
        if TEAM_ABOUT_PATH.search(path):
            preferred.append(url)
        elif INTERESTING.search(path):
            other.append(url)
    # Prefer explicit team/about paths; fill remaining slots with other interesting.
    out = preferred[:MAX_TEAM_PAGES]
    if len(out) < MAX_TEAM_PAGES:
        for u in other:
            if u not in out:
                out.append(u)
            if len(out) >= MAX_TEAM_PAGES:
                break
    return out


def fetch_domain(
    domain: str,
    session: requests.Session,
    robots: RobotsCache,
    timeout: int = 15,
    delay: float = 0.0,
) -> tuple[str, str, list[dict[str, Any]], set[str], str | None]:
    """Return (status, combined_text, page_records, emails, error).

    page_records: [{url, page_type, text}, ...]
    """
    conv = _converter()
    page_records: list[dict[str, Any]] = []
    chunks: list[str] = []
    found: set[str] = set()
    home_html = None
    home_url = ""

    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}/"
        if not robots.allows(url):
            return "skipped", "", [], set(), "robots.txt disallows /"
        home_html = _get(session, url, timeout)
        if home_html:
            home_url = url
            text = conv.handle(home_html)
            page_records.append(
                {"url": url, "page_type": "home", "text": text[:MAX_PAGE_CHARS]}
            )
            chunks.append(text)
            found |= email_lib.harvest(home_html)
            break

    if not home_html:
        return "error", "", [], set(), "homepage unreachable"

    for sub in _team_about_links(home_html, home_url):
        enough_text = sum(len(c) for c in chunks) >= MAX_CHARS
        if enough_text and found and len(page_records) > 1:
            # Still prefer fetching team pages even with enough text / email,
            # until we hit the team-page cap (already enforced by link list).
            pass
        if not robots.allows(sub):
            continue
        if delay:
            time.sleep(delay)
        html = _get(session, sub, timeout)
        if not html:
            continue
        text = conv.handle(html)
        page_type = classify_page_type(sub)
        page_records.append(
            {"url": sub, "page_type": page_type, "text": text[:MAX_PAGE_CHARS]}
        )
        found |= email_lib.harvest(html)
        if not enough_text:
            chunks.append(text)

    # Tag combined text with page_type markers so downstream extractors know
    # which section came from a team page even without site_pages rows.
    tagged_chunks = []
    for rec in page_records:
        body = (rec.get("text") or "").strip()
        if not body:
            continue
        tagged_chunks.append(f"[page_type={rec['page_type']} url={rec['url']}]\n{body}")
    text = WS.sub("\n\n", "\n\n".join(tagged_chunks or chunks)).strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    if not text:
        return "error", "", page_records, found, "no text extracted"
    return "ok", text, page_records, found, None


def fetch_team_pages_only(
    domain: str,
    session: requests.Session,
    robots: RobotsCache,
    timeout: int = 15,
    delay: float = 0.0,
) -> tuple[str, list[dict[str, Any]], set[str], str | None]:
    """Re-crawl homepage → team/about links for an already-fetched domain."""
    status, _text, pages, emails, err = fetch_domain(
        domain, session, robots, timeout=timeout, delay=delay
    )
    # Keep home + team/about; drop generic "other" for backfill focus.
    kept = [
        p for p in pages
        if p.get("page_type") in ("home", "team", "about")
        or classify_page_type(p.get("url") or "") in ("team", "about")
    ]
    return status, kept or pages, emails, err


def run(
    store: Store,
    domains: Sequence[str],
    workers: int = 12,
    timeout: int = 15,
    respect_robots: bool = True,
    delay: float = 0.0,
    on_progress: Any | None = None,
) -> dict[str, int]:
    if not domains:
        print("No pending domains.")
        return {"ok": 0, "error": 0, "skipped": 0}

    print(f"Fetching {len(domains):,} domains with {workers} workers")
    robots = RobotsCache(respect_robots)
    counts = {"ok": 0, "error": 0, "skipped": 0, "emails": 0, "pages": 0}
    lock = threading.Lock()
    done = 0

    def work(domain: str) -> None:
        nonlocal done
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Accept": "text/html"})
        try:
            status, text, pages, found, err = fetch_domain(
                domain, session, robots, timeout, delay
            )
        except Exception as exc:  # noqa: BLE001
            status, text, pages, found, err = (
                "error", "", [], set(), f"{type(exc).__name__}: {exc}"
            )
        finally:
            session.close()
        store.save_site(domain, status, text, pages, err)
        if pages:
            store.save_site_pages(domain, pages)
        if found:
            store.save_emails(domain, found, source="website")
        with lock:
            counts[status] = counts.get(status, 0) + 1
            counts["emails"] += int(bool(found))
            counts["pages"] += len(pages)
            done += 1
            if done % 10 == 0 or done == len(domains):
                sys.stderr.write(
                    f"\r  {done:,}/{len(domains):,} | ok={counts['ok']:,} "
                    f"err={counts['error']:,} skip={counts['skipped']:,} "
                    f"| with email {counts['emails']:,}   "
                )
                sys.stderr.flush()
                if on_progress is not None:
                    try:
                        on_progress(
                            done=done,
                            total=len(domains),
                            ok=counts["ok"],
                            error=counts["error"],
                            skipped=counts["skipped"],
                            emails=counts["emails"],
                        )
                    except Exception:  # noqa: BLE001
                        pass

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, d) for d in domains]
        try:
            for f in as_completed(futures):
                f.exception()
        except KeyboardInterrupt:
            print("\nInterrupted -- re-run to continue.")
            for f in futures:
                f.cancel()
    sys.stderr.write("\n")
    return counts


def crawl_team_pages(
    store: Store,
    domains: Sequence[str] | None = None,
    workers: int = 12,
    timeout: int = 15,
    respect_robots: bool = True,
    delay: float = 0.0,
    limit: int | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Backfill team/about pages for domains already in sites (status=ok)."""
    if domains is None:
        domains = (
            store.domains_with_ok_sites(limit=limit)
            if force
            else store.domains_needing_team_crawl(limit=limit)
        )
    elif limit:
        domains = list(domains)[: int(limit)]
    else:
        domains = list(domains)

    if not domains:
        return {"ok": 0, "error": 0, "skipped": 0, "pages": 0, "domains": 0}

    print(f"Team-page crawl for {len(domains):,} domains ({workers} workers)")
    robots = RobotsCache(respect_robots)
    counts = {"ok": 0, "error": 0, "skipped": 0, "pages": 0, "domains": len(domains)}
    lock = threading.Lock()
    done = 0

    def work(domain: str) -> None:
        nonlocal done
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Accept": "text/html"})
        try:
            status, pages, found, err = fetch_team_pages_only(
                domain, session, robots, timeout, delay
            )
        except Exception as exc:  # noqa: BLE001
            status, pages, found, err = (
                "error", [], set(), f"{type(exc).__name__}: {exc}"
            )
        finally:
            session.close()
        if pages:
            store.save_site_pages(domain, pages)
            # Refresh combined site text with page_type tags when crawl ok.
            if status == "ok":
                tagged = []
                for rec in pages:
                    body = (rec.get("text") or "").strip()
                    if body:
                        tagged.append(
                            f"[page_type={rec['page_type']} url={rec['url']}]\n{body}"
                        )
                combined = WS.sub("\n\n", "\n\n".join(tagged)).strip()[:MAX_CHARS]
                store.save_site(domain, "ok", combined, pages, None)
        if found:
            store.save_emails(domain, found, source="website")
        with lock:
            counts[status] = counts.get(status, 0) + 1
            counts["pages"] += len(pages)
            done += 1
            if done % 25 == 0 or done == len(domains):
                sys.stderr.write(
                    f"\r  team-crawl {done:,}/{len(domains):,} | "
                    f"ok={counts['ok']:,} pages={counts['pages']:,}   "
                )
                sys.stderr.flush()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, d) for d in domains]
        for f in as_completed(futures):
            f.exception()
    sys.stderr.write("\n")
    return counts
