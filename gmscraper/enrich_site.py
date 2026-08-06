"""Stage 3: turn a business website into plain text with html2text.

Fetches the homepage, follows at most a few in-domain links that look like
about/team/contact pages, and flattens the HTML to markdown-ish text the
local model can read.  One row per domain, so a franchise with 30 locations
is fetched once.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Sequence
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

# Link text / href fragments worth a second request.
INTERESTING = re.compile(
    r"(about|our-?story|our-?team|team|staff|leadership|management|meet|"
    r"who-?we-?are|contact|owner|founder|history|bio)",
    re.I,
)
HREF = re.compile(r'href=["\']([^"\'#]+)["\']', re.I)
WS = re.compile(r"\n{3,}")

MAX_PAGES = 4
MAX_CHARS = 12_000
MAX_BYTES = 2_000_000


def _converter() -> html2text.HTML2Text:
    h = html2text.HTML2Text()
    h.ignore_links = True
    h.ignore_images = True
    h.ignore_emphasis = True
    h.body_width = 0
    h.skip_internal_links = True
    return h


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


def _sub_pages(html: str, base: str) -> list[str]:
    """In-domain about/team/contact URLs, best few first."""
    host = urlsplit(base).netloc
    seen, out = set(), []
    for href in HREF.findall(html):
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base, href)
        if urlsplit(url).netloc != host:
            continue
        path = urlsplit(url).path
        if not path or path == "/" or not INTERESTING.search(path):
            continue
        if url in seen or len(path) > 120:
            continue
        seen.add(url)
        out.append(url)
    return out[: MAX_PAGES - 1]


def fetch_domain(
    domain: str,
    session: requests.Session,
    robots: RobotsCache,
    timeout: int = 15,
    delay: float = 0.0,
) -> tuple[str, str, list[str], set[str], str | None]:
    """Return (status, text, pages_fetched, emails, error).

    Emails are harvested from the raw HTML before html2text runs -- the
    converter drops `mailto:` hrefs, which is exactly where contact addresses
    usually live.
    """
    conv = _converter()
    pages, chunks = [], []
    found: set[str] = set()
    home_html = None

    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}/"
        if not robots.allows(url):
            return "skipped", "", [], set(), "robots.txt disallows /"
        home_html = _get(session, url, timeout)
        if home_html:
            pages.append(url)
            chunks.append(conv.handle(home_html))
            found |= email_lib.harvest(home_html)
            break

    if not home_html:
        return "error", "", [], set(), "homepage unreachable"

    # Keep visiting contact-ish pages even once we have enough text: the
    # contact page is where the email is, and it is often the last one.
    for sub in _sub_pages(home_html, pages[0]):
        enough_text = sum(len(c) for c in chunks) >= MAX_CHARS
        if enough_text and found:
            break
        if not robots.allows(sub):
            continue
        if delay:
            time.sleep(delay)
        html = _get(session, sub, timeout)
        if html:
            pages.append(sub)
            found |= email_lib.harvest(html)
            if not enough_text:
                chunks.append(conv.handle(html))

    text = WS.sub("\n\n", "\n\n".join(chunks)).strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    if not text:
        return "error", "", pages, found, "no text extracted"
    return "ok", text, pages, found, None


def run(
    store: Store,
    domains: Sequence[str],
    workers: int = 12,
    timeout: int = 15,
    respect_robots: bool = True,
    delay: float = 0.0,
) -> dict[str, int]:
    if not domains:
        print("No pending domains.")
        return {"ok": 0, "error": 0, "skipped": 0}

    print(f"Fetching {len(domains):,} domains with {workers} workers")
    robots = RobotsCache(respect_robots)
    counts = {"ok": 0, "error": 0, "skipped": 0, "emails": 0}
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
        if found:
            store.save_emails(domain, found, source="website")
        with lock:
            counts[status] = counts.get(status, 0) + 1
            counts["emails"] += int(bool(found))
            done += 1
            if done % 25 == 0 or done == len(domains):
                sys.stderr.write(
                    f"\r  {done:,}/{len(domains):,} | ok={counts['ok']:,} "
                    f"err={counts['error']:,} skip={counts['skipped']:,} "
                    f"| with email {counts['emails']:,}   "
                )
                sys.stderr.flush()

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
