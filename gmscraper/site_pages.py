"""Free website capture → public.site_pages (no LLM, no paid APIs).

Fetches homepage + up to 4 matching internal pages per domain, strips chrome,
and upserts visible text into Supabase. Counts only — never returns page body.
"""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib import error as urlerror
from urllib import parse, request
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests

from . import emails as email_lib
from . import source_binding as sb

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
TIMEOUT = 10
MAX_PAGES = 5
MAX_INTERNAL = 4
MAX_BODY = 60_000
MAX_LINKS = 200
MAX_BYTES = 2_000_000
FRESH_DAYS = 30
PAGE_SIZE = 1000
TABLE = "site_pages"
SCHEMA = "public"

PAGE_HINT = re.compile(
    r"about|services|commercial|industries|property|multifamily|projects|contact",
    re.I,
)
WS = re.compile(r"\s+")
PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}"
)
PARKED_RE = re.compile(
    r"godaddy|sedo\.com|\bsedo\b|this domain is for sale|"
    r"domain is for sale|buy this domain|hugedomains",
    re.I,
)
COMING_SOON_RE = re.compile(r"coming soon", re.I)
SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "svg", "template", "nav", "footer"}
)

SITE_PAGES_DDL = """
CREATE TABLE IF NOT EXISTS public.site_pages (
    id bigserial PRIMARY KEY,
    domain text NOT NULL,
    url text NOT NULL,
    page_type text,
    http_status int,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    title text,
    meta_description text,
    h1 text[],
    body_text text,
    links jsonb,
    emails text[],
    phones text[],
    error text,
    source_table text,
    source_id text,
    UNIQUE (domain, url)
);
CREATE INDEX IF NOT EXISTS site_pages_domain_idx ON public.site_pages (domain);
CREATE INDEX IF NOT EXISTS site_pages_source_idx ON public.site_pages (source_table, source_id);
CREATE INDEX IF NOT EXISTS site_pages_fts_idx ON public.site_pages
    USING gin (
        to_tsvector(
            'english',
            coalesce(title, '') || ' ' || coalesce(meta_description, '')
            || ' ' || coalesce(body_text, '')
        )
    );
ALTER TABLE public.site_pages ENABLE ROW LEVEL SECURITY;
GRANT ALL ON TABLE public.site_pages TO service_role;
GRANT ALL ON SEQUENCE public.site_pages_id_seq TO service_role;
NOTIFY pgrst, 'reload schema';
"""

SITE_PAGES_ALTER = """
ALTER TABLE public.site_pages ADD COLUMN IF NOT EXISTS source_table text;
ALTER TABLE public.site_pages ADD COLUMN IF NOT EXISTS source_id text;
CREATE INDEX IF NOT EXISTS site_pages_source_idx ON public.site_pages (source_table, source_id);
NOTIFY pgrst, 'reload schema';
"""


class SourceKeyError(RuntimeError):
    """Raised when SUPABASE_SERVICE_KEY_<PROJECTREF> is missing."""


def service_key_env(project_ref: str) -> str:
    return f"SUPABASE_SERVICE_KEY_{(project_ref or '').strip().upper()}"


def split_table_ref(source_table: str, default_schema: str = "public") -> tuple[str, str]:
    raw = (source_table or "").strip()
    schema = (default_schema or "public").strip() or "public"
    if not raw:
        return schema, ""
    if "." in raw:
        left, right = raw.split(".", 1)
        return (left.strip() or schema), right.strip()
    return schema, raw


def source_project_credentials(source_project: str = "") -> dict[str, str]:
    """Resolve URL + service key for a source project.

    Requires SUPABASE_SERVICE_KEY_<PROJECTREF> (uppercase). No silent fallback.
    """
    ref = (source_project or "").strip()
    if not ref:
        ref = project_ref_from_url(os.environ.get("SUPABASE_URL") or "") or ""
    if not ref:
        raise SourceKeyError(
            "source_project is empty and SUPABASE_URL has no project ref."
        )
    env_name = service_key_env(ref)
    key = (os.environ.get(env_name) or "").strip()
    if not key:
        raise SourceKeyError(
            f"Missing service key for project {ref}. Set {env_name}."
        )
    url = (os.environ.get(f"SUPABASE_URL_{ref}") or "").strip() or (
        f"https://{ref}.supabase.co"
    )
    return {
        "url": url.rstrip("/"),
        "key": key,
        "project_id": ref,
        "key_env": env_name,
    }


def project_ref_from_url(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        host = raw.split("://", 1)[-1].split("/", 1)[0]
        if host.endswith(".supabase.co"):
            return host.split(".")[0] or None
    except Exception:  # noqa: BLE001
        return None
    return None


def supabase_target(project_id: str = "") -> dict[str, str]:
    """Resolve URL/key from env. Never hardcodes a project ref."""
    creds = sb.resolve_credentials(project_id or "")
    ref = creds.get("project_id") or project_ref_from_url(creds["url"]) or ""
    return {"url": creds["url"], "key": creds["key"], "project_id": ref}


def host_of(url_or_domain: str) -> str:
    raw = (url_or_domain or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    host = (urlsplit(raw).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return ""
    return host


def classify_page_type(url: str, link_text: str = "") -> str:
    path = (urlsplit(url).path or "/").rstrip("/") or "/"
    blob = f"{path} {link_text or ''}".lower()
    if path == "/":
        return "home"
    if re.search(r"(?:^|/)(about(?:-us)?|who-we-are|our-story)(?:/|$)", path, re.I):
        return "about"
    if re.search(r"(?:^|/)(contact(?:-us)?|get-in-touch)(?:/|$)", path, re.I):
        return "contact"
    if re.search(r"(?:^|/)(services?|industries)(?:/|$)", path, re.I):
        return "services"
    if re.search(
        r"(?:^|/)(commercial|property|multifamily|multi-family|projects?)(?:/|$)",
        path,
        re.I,
    ):
        return "commercial"
    if re.search(r"\babout\b", blob):
        return "about"
    if re.search(r"\bcontact\b", blob):
        return "contact"
    if re.search(r"\b(services?|industries)\b", blob):
        return "services"
    if re.search(r"\b(commercial|property|multifamily|projects?)\b", blob):
        return "commercial"
    return "other"


def harvest_phones(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in PHONE_RE.findall(text or ""):
        digits = re.sub(r"\D", "", m)
        if len(digits) < 10 or digits in seen:
            continue
        seen.add(digits)
        found.append(m.strip())
    return found


class _PageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.host = host_of(base_url)
        self.title_parts: list[str] = []
        self.meta_description = ""
        self.h1: list[str] = []
        self.body_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._skip = 0
        self._in_title = False
        self._in_h1 = False
        self._h1_buf: list[str] = []
        self._link_href = ""
        self._link_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k.lower(): (v or "") for k, v in attrs}
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (ad.get("name") or ad.get("property") or "").lower()
            if name in ("description", "og:description") and not self.meta_description:
                self.meta_description = (ad.get("content") or "").strip()
        elif tag == "h1":
            self._in_h1 = True
            self._h1_buf = []
        elif tag == "a":
            href = (ad.get("href") or "").strip()
            if href and not href.startswith(("mailto:", "tel:", "javascript:", "#")):
                self._link_href = href
                self._link_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "h1" and self._in_h1:
            text = WS.sub(" ", "".join(self._h1_buf)).strip()
            if text:
                self.h1.append(text[:300])
            self._in_h1 = False
        elif tag == "a" and self._link_href:
            abs_url = urljoin(self.base, self._link_href)
            if host_of(abs_url) == self.host and len(self.links) < MAX_LINKS:
                self.links.append(
                    {
                        "href": _canon_url(abs_url),
                        "text": WS.sub(" ", "".join(self._link_buf)).strip()[:200],
                    }
                )
            self._link_href = ""

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self._h1_buf.append(data)
        if self._link_href:
            self._link_buf.append(data)
        self.body_parts.append(data)


def _canon_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, parts.query, ""))


def parse_page(html: str, url: str) -> dict[str, Any]:
    parser = _PageParser(url)
    try:
        parser.feed(html or "")
    except Exception:  # noqa: BLE001
        pass
    body = WS.sub(" ", " ".join(parser.body_parts)).strip()[:MAX_BODY]
    title = WS.sub(" ", "".join(parser.title_parts)).strip()[:500]
    return {
        "title": title or None,
        "meta_description": (parser.meta_description or None),
        "h1": parser.h1[:20],
        "body_text": body or None,
        "links": parser.links[:MAX_LINKS],
    }


def looks_parked(html: str, body_text: str) -> bool:
    blob = f"{html or ''}\n{body_text or ''}"
    if PARKED_RE.search(blob):
        return True
    text = (body_text or "").strip()
    return bool(COMING_SOON_RE.search(text) and len(text) < 200)


def contains_html_markup(text: str | None) -> bool:
    if not text:
        return False
    return bool(re.search(r"</?(script|style|html|body|div|nav|footer)\b", text, re.I))


def pick_internal_targets(home_url: str, links: list[dict[str, str]]) -> list[str]:
    home = _canon_url(home_url)
    out: list[str] = []
    seen = {home}
    for link in links:
        href = link.get("href") or ""
        text = link.get("text") or ""
        if not href or href in seen:
            continue
        path = urlsplit(href).path or ""
        if PAGE_HINT.search(path) or PAGE_HINT.search(text):
            seen.add(href)
            out.append(href)
        if len(out) >= MAX_INTERNAL:
            break
    return out


class RobotsCache:
    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allows(self, url: str) -> bool:
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
            r = requests.get(robots, headers={"User-Agent": UA}, timeout=TIMEOUT)
            if r.status_code != 200 or not (r.text or "").strip():
                return None
            rp = RobotFileParser()
            rp.parse(r.text.splitlines())
            return rp
        except requests.RequestException:
            return None


def _classify_transport_error(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if isinstance(exc, (requests.Timeout, TimeoutError)) or "timed out" in msg:
        return "timeout"
    if (
        isinstance(exc, requests.exceptions.ConnectionError)
        or "name or service not known" in msg
        or "nodename nor servname" in msg
        or "failed to resolve" in msg
        or "nameresolution" in name
        or "gaierror" in msg
        or "getaddrinfo" in msg
    ):
        return "dns"
    return type(exc).__name__


def fetch_one(
    session: requests.Session,
    url: str,
    robots: RobotsCache,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "url": _canon_url(url),
        "http_status": None,
        "title": None,
        "meta_description": None,
        "h1": [],
        "body_text": None,
        "links": [],
        "emails": [],
        "phones": [],
        "error": None,
        "page_type": classify_page_type(url),
    }
    if not robots.allows(url):
        row["error"] = "robots"
        return row
    html = ""
    try:
        r = session.get(
            url,
            timeout=TIMEOUT,
            allow_redirects=True,
            stream=True,
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"},
        )
        row["http_status"] = int(r.status_code)
        row["url"] = _canon_url(str(r.url or url))
        if r.status_code == 403:
            row["error"] = "403"
        raw = r.raw.read(MAX_BYTES, decode_content=True) or b""
        html = raw.decode(r.encoding or "utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        row["error"] = row["error"] or _classify_transport_error(exc)
        return row
    finally:
        try:
            r.close()  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass

    parsed = parse_page(html, row["url"])
    row.update(parsed)
    emails = sorted(email_lib.harvest(html) | email_lib.harvest(row.get("body_text") or ""))
    phones = harvest_phones(f"{html}\n{row.get('body_text') or ''}")
    row["emails"] = emails
    row["phones"] = phones
    if looks_parked(html, row.get("body_text") or ""):
        row["error"] = "parked"
    elif row["http_status"] and int(row["http_status"]) >= 400 and not row["error"]:
        row["error"] = str(row["http_status"])
    if contains_html_markup(row.get("body_text")):
        row["body_text"] = WS.sub(" ", re.sub(r"<[^>]+>", " ", row["body_text"] or "")).strip()[
            :MAX_BODY
        ]
    return row


def crawl_domain(domain: str, robots: RobotsCache | None = None) -> list[dict[str, Any]]:
    host = host_of(domain)
    if not host:
        return [
            {
                "domain": domain,
                "url": domain or "",
                "page_type": "other",
                "http_status": None,
                "title": None,
                "meta_description": None,
                "h1": [],
                "body_text": None,
                "links": [],
                "emails": [],
                "phones": [],
                "error": "invalid_domain",
            }
        ]
    robots = robots or RobotsCache()
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "text/html"})
    pages: list[dict[str, Any]] = []
    try:
        home = None
        home_url = ""
        last_err = None
        for scheme in ("https", "http"):
            url = f"{scheme}://{host}/"
            rec = fetch_one(session, url, robots)
            rec["domain"] = host
            rec["page_type"] = "home"
            last_err = rec
            if rec.get("http_status") or rec.get("body_text") or rec.get("error") == "robots":
                home = rec
                home_url = rec.get("url") or url
                if rec.get("error") in ("dns", "timeout") and scheme == "https":
                    continue
                if rec.get("body_text") or rec.get("error") in ("403", "parked", "robots"):
                    break
                if rec.get("http_status"):
                    break
        if home is None:
            fail = last_err or {
                "domain": host,
                "url": f"https://{host}/",
                "page_type": "home",
                "http_status": None,
                "error": "unreachable",
                "title": None,
                "meta_description": None,
                "h1": [],
                "body_text": None,
                "links": [],
                "emails": [],
                "phones": [],
            }
            fail["domain"] = host
            return [fail]
        pages.append(home)
        if home.get("error") in ("dns", "timeout", "parked", "robots"):
            return pages
        for target in pick_internal_targets(home_url, home.get("links") or []):
            if len(pages) >= MAX_PAGES:
                break
            rec = fetch_one(session, target, robots)
            rec["domain"] = host
            rec["page_type"] = classify_page_type(rec.get("url") or target)
            pages.append(rec)
        return pages
    finally:
        session.close()


def parse_domain_list(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = raw
    else:
        parts = re.split(r"[\s,;]+", str(raw))
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        h = host_of(p)
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def fetch_source_rows(
    cfg: dict[str, str],
    *,
    schema: str = "public",
    table: str = "",
    where: str = "",
    domain_column: str = "domain",
    key_column: str = "id",
    limit: int = 0,
    skip_domains: set[str] | None = None,
    already_fn: Any | None = None,
) -> list[dict[str, str]]:
    """Page key+domain from a table or view, 1,000 rows at a time.

    Returns [{domain, source_id}]. skip_domains are omitted so a daily rerun
    only picks up new hosts. Stops once `limit` unique domains are collected.
    """
    if not table:
        return []
    schema, table = split_table_ref(table, schema or "public")
    if not table:
        return []
    sb._require_ident(schema, "schema")
    sb._require_ident(table, "table")
    domain_column = (domain_column or "domain").strip()
    key_column = (key_column or "id").strip()
    sb._require_ident(domain_column, "column")
    sb._require_ident(key_column, "column")
    skip = {d.lower() for d in (skip_domains or set())}
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    offset = 0
    cap = int(limit) if limit and limit > 0 else 0
    clause = (where or "").strip() or None
    cols = [key_column, domain_column]
    use_rpc = True
    while True:
        page: list[Any] = []
        if use_rpc:
            try:
                raw = _rpc(
                    cfg,
                    "pp_select_rows",
                    {
                        "p_schema": schema,
                        "p_table": table,
                        "p_columns": cols,
                        "p_where": clause,
                        "p_order_by": key_column,
                        "p_limit": PAGE_SIZE,
                        "p_offset": offset,
                    },
                )
                page = raw if isinstance(raw, list) else []
            except RuntimeError:
                use_rpc = False
        if not use_rpc:
            if clause:
                raise RuntimeError(
                    f"pp_select_rows is unavailable on {cfg.get('project_id')}; "
                    "cannot apply a SQL where on PostgREST fallback. "
                    "Install pp_select_rows or omit where."
                )
            path = (
                f"{table}?select={key_column},{domain_column}"
                f"&order={key_column}.asc&limit={PAGE_SIZE}&offset={offset}"
            )
            _status, body = _rest(
                cfg, "GET", path, prefer="return=representation", schema=schema
            )
            try:
                page = json.loads(body or "[]")
            except json.JSONDecodeError:
                page = []
            if not isinstance(page, list):
                page = []
        if not page:
            break
        if already_fn is not None:
            page_hosts = []
            for row in page:
                if isinstance(row, dict):
                    h = host_of(str(row.get(domain_column) or ""))
                    if h:
                        page_hosts.append(h)
            try:
                skip |= {d.lower() for d in (already_fn(page_hosts) or set())}
            except Exception:  # noqa: BLE001
                pass
        for row in page:
            if not isinstance(row, dict):
                continue
            h = host_of(str(row.get(domain_column) or ""))
            if not h or h in seen or h in skip:
                continue
            seen.add(h)
            sid = row.get(key_column)
            out.append({"domain": h, "source_id": "" if sid is None else str(sid)})
            if cap and len(out) >= cap:
                return out
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


def fetch_source_domains(
    *,
    schema: str = "public",
    table: str = "",
    where: str = "",
    domain_column: str = "domain",
    website_column: str = "website",
    project_id: str = "",
    limit: int = 0,
) -> list[str]:
    """Backward-compatible host list. Prefer fetch_source_rows."""
    cfg = supabase_target(project_id)
    rows = fetch_source_rows(
        cfg,
        schema=schema,
        table=table,
        where=where,
        domain_column=domain_column or website_column or "domain",
        key_column="id",
        limit=limit,
    )
    return [r["domain"] for r in rows]


def _headers(
    cfg: dict[str, str],
    *,
    prefer: str = "return=minimal",
    schema: str = SCHEMA,
) -> dict[str, str]:
    profile = schema or SCHEMA
    return {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
        "Prefer": prefer,
        "Accept-Profile": profile,
        "Content-Profile": profile,
    }


def _rest(
    cfg: dict[str, str],
    method: str,
    path: str,
    *,
    body: Any = None,
    prefer: str = "return=minimal",
    timeout: int = 60,
    schema: str = SCHEMA,
) -> tuple[int, str]:
    url = f"{cfg['url']}/rest/v1/{path.lstrip('/')}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers=_headers(cfg, prefer=prefer, schema=schema),
        method=method,
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase {method} {url} failed ({exc.code}): {detail[:400]}"
        ) from exc


def _rpc(cfg: dict[str, str], fn: str, args: dict[str, Any]) -> Any:
    _status, body = _rest(
        cfg,
        "POST",
        f"rpc/{fn}",
        body=args,
        prefer="return=representation",
        schema="public",
    )
    if not body:
        return []
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return []


def ensure_table(cfg: dict[str, str]) -> dict[str, Any]:
    try:
        _rest(cfg, "GET", f"{TABLE}?select=id,source_id,source_table&limit=1")
        return {"ok": True, "apply_sql": None}
    except RuntimeError as exc:
        msg = str(exc)
        missing_col = (
            "source_id" in msg or "source_table" in msg or "42703" in msg or "PGRST204" in msg
        )
        missing_table = (
            "PGRST205" in msg or "does not exist" in msg.lower()
        )
        if missing_table:
            return {"ok": False, "apply_sql": SITE_PAGES_DDL, "error": msg[:300]}
        if missing_col:
            return {"ok": False, "apply_sql": SITE_PAGES_ALTER, "error": msg[:300]}
        if "404" in msg:
            return {"ok": False, "apply_sql": SITE_PAGES_DDL, "error": msg[:300]}
        raise


def recently_fetched(cfg: dict[str, str], domains: list[str]) -> set[str]:
    if not domains:
        return set()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)).isoformat()
    fresh: set[str] = set()
    for i in range(0, len(domains), 80):
        chunk = domains[i : i + 80]
        quoted = ",".join(parse.quote(d, safe="") for d in chunk)
        path = (
            f"{TABLE}?select=domain&domain=in.({quoted})"
            f"&fetched_at=gte.{parse.quote(cutoff)}"
        )
        try:
            _status, body = _rest(cfg, "GET", path, prefer="return=representation")
        except RuntimeError:
            continue
        try:
            rows = json.loads(body or "[]")
        except json.JSONDecodeError:
            rows = []
        for row in rows:
            if isinstance(row, dict) and row.get("domain"):
                fresh.add(str(row["domain"]).lower())
    return fresh


def existing_domains(cfg: dict[str, str], domains: list[str]) -> set[str]:
    """Domains already present in site_pages (any fetched_at)."""
    if not domains:
        return set()
    found: set[str] = set()
    for i in range(0, len(domains), 80):
        chunk = domains[i : i + 80]
        quoted = ",".join(parse.quote(d, safe="") for d in chunk)
        path = f"{TABLE}?select=domain&domain=in.({quoted})"
        try:
            _status, body = _rest(cfg, "GET", path, prefer="return=representation")
        except RuntimeError:
            continue
        try:
            rows = json.loads(body or "[]")
        except json.JSONDecodeError:
            rows = []
        for row in rows:
            if isinstance(row, dict) and row.get("domain"):
                found.add(str(row["domain"]).lower())
    return found


def _row_payload(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "domain": rec.get("domain") or "",
        "url": rec.get("url") or "",
        "page_type": rec.get("page_type") or "other",
        "http_status": rec.get("http_status"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "title": rec.get("title"),
        "meta_description": rec.get("meta_description"),
        "h1": rec.get("h1") or [],
        "body_text": rec.get("body_text"),
        "links": rec.get("links") or [],
        "emails": rec.get("emails") or [],
        "phones": rec.get("phones") or [],
        "error": rec.get("error"),
        "source_table": rec.get("source_table"),
        "source_id": rec.get("source_id"),
    }


def upsert_pages(cfg: dict[str, str], rows: list[dict[str, Any]]) -> int:
    payload = [_row_payload(r) for r in rows if r.get("domain") and r.get("url")]
    if not payload:
        return 0
    synced = 0
    for i in range(0, len(payload), 200):
        batch = payload[i : i + 200]
        _rest(
            cfg,
            "POST",
            f"{TABLE}?on_conflict=domain,url",
            body=batch,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        synced += len(batch)
    return synced


def crawl_and_store(
    *,
    domains: str | list[str] | None = None,
    schema: str = "public",
    table: str = "",
    where: str = "",
    domain_column: str = "domain",
    website_column: str = "website",
    key_column: str = "id",
    source_project: str = "",
    project_id: str = "",
    limit: int = 0,
    force: bool = False,
    workers: int = 20,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    source_table = (table or "").strip()
    project_ref = (source_project or project_id or "").strip()
    source_ids: dict[str, str] = {}
    source_qual = ""

    if source_table:
        try:
            cfg = source_project_credentials(project_ref)
        except SourceKeyError as exc:
            return {
                "ok": False,
                "domains_attempted": 0,
                "domains_skipped_fresh": 0,
                "domains_skipped": 0,
                "pages_stored": 0,
                "errors_by_type": {},
                "supabase_project_ref": project_ref or None,
                "source_project": project_ref or None,
                "source_table": source_table,
                "table": f"{SCHEMA}.{TABLE}",
                "error": str(exc),
            }
        schema, tbl = split_table_ref(source_table, schema or "public")
        source_qual = f"{schema}.{tbl}" if tbl else source_table
    else:
        cfg = supabase_target(project_ref)
        schema, tbl = (schema or "public"), ""

    def _fail(error: str, apply_sql: str | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "domains_attempted": 0,
            "domains_skipped_fresh": 0,
            "domains_skipped": 0,
            "pages_stored": 0,
            "errors_by_type": {},
            "supabase_project_ref": cfg.get("project_id") or None,
            "source_project": cfg.get("project_id") or None,
            "source_table": source_qual or None,
            "table": f"{SCHEMA}.{TABLE}",
            "apply_sql": apply_sql,
            "error": error,
        }

    probe = ensure_table(cfg)
    if not probe.get("ok"):
        return _fail(
            "public.site_pages is missing columns. Apply apply_sql on this project.",
            probe.get("apply_sql"),
        )

    skipped = 0
    hosts: list[str] = []
    if source_table:
        skipped_box = [0]

        def already_fn(ds: list[str]) -> set[str]:
            found = existing_domains(cfg, ds)
            skipped_box[0] += len(found)
            return found

        try:
            rows = fetch_source_rows(
                cfg,
                schema=schema,
                table=tbl or source_table,
                where=where,
                domain_column=domain_column or "domain",
                key_column=key_column or "id",
                limit=int(limit or 0),
                already_fn=None if force else already_fn,
            )
        except SourceKeyError as exc:
            return _fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        for rec in rows:
            h = rec["domain"]
            source_ids[h] = rec.get("source_id") or ""
            hosts.append(h)
        skipped = skipped_box[0]
    else:
        hosts = parse_domain_list(domains)
        if limit and len(hosts) > int(limit):
            hosts = hosts[: int(limit)]
        if hosts and not force:
            fresh = recently_fetched(cfg, hosts)
            if fresh:
                hosts = [h for h in hosts if h not in fresh]
                skipped = len(fresh)

    errors: dict[str, int] = {}
    pages_stored = 0
    attempted = 0
    lock = threading.Lock()
    robots = RobotsCache()
    n_workers = max(1, min(int(workers or 20), 20))

    def _stamp(recs: list[dict[str, Any]], host: str) -> list[dict[str, Any]]:
        sid = source_ids.get(host)
        for rec in recs:
            rec["domain"] = rec.get("domain") or host
            if source_qual:
                rec["source_table"] = source_qual
                rec["source_id"] = sid
        return recs

    def work(host: str) -> None:
        nonlocal pages_stored, attempted
        recs = _stamp(crawl_domain(host, robots), host)
        stored = 0
        try:
            stored = upsert_pages(cfg, recs)
        except Exception as exc:  # noqa: BLE001
            recs = _stamp(
                [
                    {
                        "domain": host,
                        "url": f"https://{host}/",
                        "page_type": "home",
                        "http_status": None,
                        "title": None,
                        "meta_description": None,
                        "h1": [],
                        "body_text": None,
                        "links": [],
                        "emails": [],
                        "phones": [],
                        "error": f"upsert:{type(exc).__name__}",
                    }
                ],
                host,
            )
            try:
                stored = upsert_pages(cfg, recs)
            except Exception:  # noqa: BLE001
                stored = 0
        with lock:
            attempted += 1
            pages_stored += stored
            for rec in recs:
                err = rec.get("error")
                if err:
                    errors[str(err)] = errors.get(str(err), 0) + 1
            if on_progress is not None:
                try:
                    on_progress(
                        done=attempted,
                        total=len(hosts),
                        pages_stored=pages_stored,
                        errors=sum(errors.values()),
                    )
                except Exception:  # noqa: BLE001
                    pass

    if hosts:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futs = [pool.submit(work, h) for h in hosts]
            for f in as_completed(futs):
                f.exception()

    return {
        "ok": True,
        "domains_attempted": attempted,
        "domains_skipped_fresh": skipped,
        "domains_skipped": skipped,
        "pages_stored": pages_stored,
        "errors_by_type": errors,
        "supabase_project_ref": cfg.get("project_id") or None,
        "source_project": cfg.get("project_id") or None,
        "source_table": source_qual or None,
        "table": f"{SCHEMA}.{TABLE}",
        "force": bool(force),
        "workers": n_workers,
    }
