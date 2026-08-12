"""Apify contact-info-scraper crawl + OpenAI person parsing.

Default actor: vdrmota/contact-info-scraper.
Never returns row payloads to MCP callers — counts / run_id only.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import requests

from . import gc_sync
from .config import settings
from .llm import OllamaError, make_llm
from .store import Store

# Pricing model (FREE tier) for vdrmota/contact-info-scraper
# https://apify.com/vdrmota/contact-info-scraper/pricing
COST_START_USD = 0.001  # Actor start per 1 GB
COST_PAGE_USD = 0.002  # scraped page
# Email-verify / leads-enrichment add-ons stay OFF (expensive); verify_emails is ignored.

POLL_INTERVAL_SEC = 5.0
POLL_MAX_WAIT_SEC = 60 * 45  # 45 minutes

ENTITY_MARKERS = re.compile(
    r"\b(llc|inc\.?|corp\.?|ltd\.?|company|group|construction|builders?"
    r"|contractors?|services|partners?|development|corporation)\b",
    re.I,
)
TITLE_ONLY = re.compile(
    r"^(project\s+manager|manager|president|ceo|owner|director|estimator|"
    r"superintendent|administrator|assistant|coordinator|engineer)$",
    re.I,
)

PARSE_SYSTEM = """You extract decision-maker people from company website crawl text.
Return ONLY valid JSON matching the schema. No prose, no markdown fences.

Rules:
1. Return a person only when a real human name is present. Never put a job title,
   department, or company name in first_name/last_name. If the page shows only
   "Project Manager" with no name, omit that entry.
2. Reject entity names. If the candidate contains LLC, Inc, Corp, Ltd, Company,
   Group, Construction, Builders, Contractors, Services, Partners, Development,
   treat it as a company and skip it.
3. Do not invent or pattern-generate email addresses. Only return an email that
   appears verbatim in the page text. Never construct first.last@domain.com.
4. Prefer decision makers: owner, president, CEO, principal, partner, vice
   president, director, preconstruction, estimating, project executive, business
   development. Skip field staff, administrative assistants, and general info
   inboxes.
5. Names in image alt text and figure captions count; team pages are often photo grids.
6. Set confidence between 0 and 1 for how clearly the name and title were stated.
7. Return {"people": []} when nothing qualifies. Empty is correct and expected.
"""

PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "job_title": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "linkedin_url": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "first_name", "last_name", "job_title", "email",
                    "phone", "linkedin_url", "confidence",
                ],
            },
        }
    },
    "required": ["people"],
}


def estimate_cost_usd(
    n_urls: int,
    verify_emails: bool = False,
    max_pages_per_site: int = 3,
) -> float:
    """Upper-bound FREE-tier estimate: start + pages × maxRequestsPerStartUrl.

    Default depth 3 = homepage + about + team. verify_emails is accepted for
    API compat but does not enable the paid leads-enrichment add-ons.
    """
    del verify_emails
    n = max(0, int(n_urls))
    pages_per = max(1, int(max_pages_per_site or 3))
    return COST_START_USD + COST_PAGE_USD * n * pages_per


def apify_token_valid(token: str | None = None, timeout: int = 15) -> bool:
    tok = (token if token is not None else settings.apify_token) or ""
    if not tok.strip():
        return False
    try:
        r = requests.get(
            f"{settings.apify_base_url}/v2/users/me",
            params={"token": tok},
            timeout=timeout,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def _actor_id(actor: str) -> str:
    return actor.strip().replace("/", "~")


def _normalize_url(domain_or_url: str) -> str:
    raw = (domain_or_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    return f"https://{host}"


def _domain_from_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def domains_from_source(store: Store, source: str, limit: int = 0) -> list[str]:
    """Select domains from local SQLite by source tag."""
    src = (source or "").strip().lower()
    if src in ("maps_no_owner", "no_owner", "maps_missing_owner"):
        # Domains with a website but no usable person (owners / prior DM tiers).
        sql = """
            SELECT DISTINCT b.domain FROM businesses b
            WHERE b.domain IS NOT NULL AND b.domain != ''
              AND b.website IS NOT NULL AND b.website != ''
              AND COALESCE(b.source, 'maps') = 'maps'
              AND NOT EXISTS (
                SELECT 1 FROM owners o
                WHERE o.place_id = b.place_id
                  AND o.owner_name IS NOT NULL AND o.owner_name != ''
              )
              AND NOT EXISTS (
                SELECT 1 FROM contacts c
                WHERE c.domain = b.domain
                  AND c.name IS NOT NULL AND c.name != ''
                  AND COALESCE(c.source_tier, '') IN (
                    'apify_openai', 'getleads', 'ai_ark', 'leadmagic'
                  )
              )
            ORDER BY b.domain
        """
    elif src in ("icp_no_owner", "maps_icp_no_owner"):
        sql = """
            SELECT DISTINCT b.domain FROM businesses b
            JOIN verdicts v ON v.place_id = b.place_id AND v.in_icp = 1
            WHERE b.domain IS NOT NULL AND b.domain != ''
              AND NOT EXISTS (
                SELECT 1 FROM owners o
                WHERE o.place_id = b.place_id
                  AND o.owner_name IS NOT NULL AND o.owner_name != ''
              )
            ORDER BY b.domain
        """
    else:
        raise ValueError(
            f"Unknown source={source!r}. Use maps_no_owner / icp_no_owner, "
            "or pass domains= explicitly."
        )
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    return [r["domain"] for r in store.conn.execute(sql)]


def resolve_urls(
    store: Store | None,
    *,
    domains: str = "",
    source: str = "",
    limit: int = 0,
) -> list[str]:
    urls: list[str] = []
    if domains.strip():
        parts = re.split(r"[\s,]+", domains.strip())
        urls = [_normalize_url(p) for p in parts if p.strip()]
    elif source.strip():
        if store is None:
            raise ValueError("source= requires a local Store")
        urls = [
            _normalize_url(d)
            for d in domains_from_source(store, source, limit=limit)
        ]
    else:
        raise ValueError("Pass domains= or source=")
    urls = [u for u in urls if u]
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        d = _domain_from_url(u)
        if d in seen:
            continue
        seen.add(d)
        out.append(u)
    if limit and limit > 0:
        out = out[: int(limit)]
    return out


# Paid social-profile enrichment stays OFF — flat false object satisfies the
# current actor schema (object required; boolean 400s since ~Aug 2026).
_SOCIAL_PROFILES_OFF: dict[str, bool] = {
    "facebooks": False,
    "instagrams": False,
    "youtubes": False,
    "tiktoks": False,
    "twitters": False,
}


def build_contact_actor_input(
    urls: list[str],
    *,
    max_pages_per_site: int = 3,
    use_proxy: bool = True,
) -> dict[str, Any]:
    """Build vdrmota/contact-info-scraper input matching the current schema.

    Validates required shapes so we fail in-process instead of Apify 400s.
    Paid leads-enrichment / social-profile add-ons stay disabled.
    """
    pages_per = max(1, int(max_pages_per_site or 3))
    start_urls = [{"url": u} for u in urls if u]
    if not start_urls:
        raise ValueError("build_contact_actor_input requires at least one URL")

    run_input: dict[str, Any] = {
        "startUrls": start_urls,
        "maxRequestsPerStartUrl": pages_per,
        "mergeContacts": True,
        "maxDepth": 2,
        "sameDomain": True,
        "considerChildFrames": False,
        # Keep paid leads / email-verify / social add-ons OFF.
        "maximumLeadsEnrichmentRecords": 0,
        "verifyLeadsEnrichmentEmails": False,
        "scrapeSocialMediaProfiles": dict(_SOCIAL_PROFILES_OFF),
        "useBrowser": False,
        "waitUntil": "domcontentloaded",
        "proxyConfig": {"useApifyProxy": bool(use_proxy)},
    }
    validate_contact_actor_input(run_input)
    return run_input


def validate_contact_actor_input(run_input: dict[str, Any]) -> None:
    """Raise ValueError if payload would 400 against the actor input schema."""
    if not isinstance(run_input.get("startUrls"), list) or not run_input["startUrls"]:
        raise ValueError("startUrls must be a non-empty list of {url} objects")
    for item in run_input["startUrls"]:
        if not isinstance(item, dict) or not str(item.get("url") or "").strip():
            raise ValueError(f"startUrls entries must be {{url: ...}}; got {item!r}")

    social = run_input.get("scrapeSocialMediaProfiles")
    if not isinstance(social, dict):
        raise ValueError(
            "scrapeSocialMediaProfiles must be an object "
            f"(facebooks/instagrams/…); got {type(social).__name__}"
        )
    for key in _SOCIAL_PROFILES_OFF:
        if key not in social:
            raise ValueError(f"scrapeSocialMediaProfiles missing key {key!r}")
        if not isinstance(social[key], bool):
            raise ValueError(
                f"scrapeSocialMediaProfiles.{key} must be bool; got {social[key]!r}"
            )

    proxy = run_input.get("proxyConfig")
    if proxy is not None and not isinstance(proxy, dict):
        raise ValueError("proxyConfig must be an object")

    # Legacy boolean flag removed from schema — reject if callers sneak it in.
    if "leadsEnrichment" in run_input and not isinstance(
        run_input.get("leadsEnrichment"), (list, type(None))
    ):
        # Actor no longer accepts a bare boolean; strip would be silent — fail loud.
        if isinstance(run_input.get("leadsEnrichment"), bool):
            raise ValueError(
                "leadsEnrichment boolean is invalid; use "
                "maximumLeadsEnrichmentRecords=0 to disable paid leads"
            )

    for int_key in ("maxRequestsPerStartUrl", "maxDepth", "maximumLeadsEnrichmentRecords"):
        if int_key in run_input and not isinstance(run_input[int_key], int):
            raise ValueError(f"{int_key} must be int")


def crawl(
    store: Store,
    *,
    domains: str = "",
    source: str = "",
    limit: int = 0,
    max_pages_per_site: int = 3,
    verify_emails: bool = False,
    use_proxy: bool = True,
    estimate_only: bool = False,
    run_label: str = "",
) -> dict[str, Any]:
    urls = resolve_urls(store, domains=domains, source=source, limit=limit)
    pages_per = max(1, int(max_pages_per_site or 3))
    estimated = round(
        estimate_cost_usd(
            len(urls),
            verify_emails=verify_emails,
            max_pages_per_site=pages_per,
        ),
        6,
    )
    max_cost = float(settings.apify_max_cost_usd or 0.0)
    base = {
        "domains": len(urls),
        "estimated_cost_usd": estimated,
        "max_cost_usd": max_cost if max_cost > 0 else None,
        "cost_ceiling": "none" if max_cost <= 0 else f"${max_cost:.2f}",
        "max_pages_per_site": pages_per,
        "verify_emails": bool(verify_emails),
        "actor": settings.apify_contact_actor,
        "run_label": run_label or "",
        "estimate_only": bool(estimate_only),
    }
    if not urls:
        return {**base, "started": False, "reason": "no_urls"}

    if estimate_only:
        return {**base, "started": False, "blocked": False}

    if max_cost > 0 and estimated > max_cost:
        return {
            **base,
            "started": False,
            "blocked": True,
            "reason": f"estimate ${estimated:.4f} exceeds APIFY_MAX_COST_USD ${max_cost:.2f}",
        }

    token = settings.apify_token
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set")
    if not apify_token_valid(token):
        raise RuntimeError("APIFY_TOKEN failed validation against /v2/users/me")

    actor = _actor_id(settings.apify_contact_actor)
    run_input = build_contact_actor_input(
        urls,
        max_pages_per_site=pages_per,
        use_proxy=bool(use_proxy),
    )

    start_url = f"{settings.apify_base_url}/v2/acts/{actor}/runs"
    # max_cost <= 0 = no global ceiling; still bound this single actor run.
    run_charge = max_cost if max_cost > 0 else max(float(estimated) * 3.0, 1.0)
    params = {
        "token": token,
        "maxTotalChargeUsd": run_charge,
        "timeout": 600,
        "memory": 1024,
    }
    resp = requests.post(
        start_url,
        params=params,
        json=run_input,
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Apify start failed ({resp.status_code}): {resp.text[:400]}"
        )
    run = (resp.json() or {}).get("data") or {}
    run_id = run.get("id") or ""
    if not run_id:
        raise RuntimeError(f"Apify start returned no run id: {resp.text[:400]}")

    terminal = _poll_run(run_id, token)
    dataset_id = terminal.get("defaultDatasetId") or ""
    items = _fetch_dataset_items(dataset_id, token) if dataset_id else []
    persisted = store.save_apify_contact_raw(run_id, items, run_label=run_label)

    usage = terminal.get("usageTotalUsd")
    try:
        actual_cost = float(usage) if usage is not None else None
    except (TypeError, ValueError):
        actual_cost = None

    return {
        **base,
        "started": True,
        "blocked": False,
        "run_id": run_id,
        "status": terminal.get("status"),
        "items_fetched": len(items),
        "rows_persisted": persisted,
        "actual_cost_usd": actual_cost,
        "dataset_id": dataset_id,
    }


def _poll_run(run_id: str, token: str) -> dict[str, Any]:
    url = f"{settings.apify_base_url}/v2/actor-runs/{run_id}"
    deadline = time.time() + POLL_MAX_WAIT_SEC
    last: dict[str, Any] = {}
    while time.time() < deadline:
        r = requests.get(url, params={"token": token}, timeout=30)
        if r.status_code >= 400:
            time.sleep(POLL_INTERVAL_SEC)
            continue
        last = (r.json() or {}).get("data") or {}
        status = str(last.get("status") or "")
        if status in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
            return last
        time.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError(f"Apify run {run_id} did not finish within {POLL_MAX_WAIT_SEC}s")


def _fetch_dataset_items(dataset_id: str, token: str) -> list[dict[str, Any]]:
    url = f"{settings.apify_base_url}/v2/datasets/{dataset_id}/items"
    items: list[dict[str, Any]] = []
    offset = 0
    limit = 250
    while True:
        r = requests.get(
            url,
            params={
                "token": token,
                "clean": "true",
                "format": "json",
                "limit": limit,
                "offset": offset,
            },
            timeout=120,
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"Dataset fetch failed ({r.status_code}): {r.text[:300]}"
            )
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        for row in batch:
            if isinstance(row, dict):
                items.append(row)
        if len(batch) < limit:
            break
        offset += limit
    return items


def _item_domain(item: dict[str, Any]) -> str:
    for key in (
        "domain",
        "website",
        "url",
        "originalStartUrl",
        "inputUrl",
        "startUrl",
        "loadedUrl",
    ):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            if "://" in val or "/" in val:
                return _domain_from_url(_normalize_url(val))
            return val.strip().lower().lstrip("www.")
    scraped = item.get("scrapedUrls")
    if isinstance(scraped, list) and scraped:
        first = scraped[0]
        if isinstance(first, str) and first.strip():
            return _domain_from_url(_normalize_url(first))
    return ""


def _item_url(item: dict[str, Any]) -> str:
    for key in (
        "originalStartUrl",
        "url",
        "loadedUrl",
        "startUrl",
        "inputUrl",
        "website",
    ):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    scraped = item.get("scrapedUrls")
    if isinstance(scraped, list):
        for u in scraped:
            if isinstance(u, str) and u.strip():
                return u.strip()
    domain = _item_domain(item)
    return f"https://{domain}" if domain else ""


def _item_text(item: dict[str, Any]) -> str:
    """Best-effort page text / markdown / contact blob for the LLM."""
    chunks: list[str] = []
    for key in (
        "markdown", "text", "pageText", "content", "html", "body",
        "contactPageText", "aboutPageText", "teamPageText",
    ):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            chunks.append(val.strip())
    # Structured contact fields (vdrmota/contact-info-scraper shape).
    for key in (
        "emails",
        "phones",
        "phonesUncertain",
        "linkedIns",
        "facebooks",
        "twitters",
        "instagrams",
        "leadsEnrichment",
        "socials",
        "people",
        "contacts",
        "scrapedUrls",
    ):
        val = item.get(key)
        if val:
            chunks.append(f"{key}: {json.dumps(val, ensure_ascii=False)[:4000]}")
    # Fall back to a compact JSON view of the item.
    if not chunks:
        chunks.append(json.dumps(item, ensure_ascii=False)[:8000])
    text = "\n\n".join(chunks)
    return text[:20_000]


def _looks_like_person(first: str, last: str) -> bool:
    name = f"{first} {last}".strip()
    if len(name) < 3:
        return False
    if TITLE_ONLY.match(name.strip()):
        return False
    if ENTITY_MARKERS.search(name):
        return False
    # Require at least one alpha token that isn't a title word
    if not re.search(r"[A-Za-z]{2,}", first) and not re.search(r"[A-Za-z]{2,}", last):
        return False
    # Reject single-letter surname only? Steve W. is weak but allowed with low conf
    if first.lower() in {"project", "general", "construction", "the"}:
        return False
    return True


def _email_on_page(email: str, page_text: str) -> bool:
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return False
    return e in (page_text or "").lower()


def parse_contacts_openai(
    store: Store,
    *,
    run_id: str = "",
    source: str = "",
    limit: int = 0,
    model: str = "gpt-4o-mini",
    workers: int = 8,
) -> dict[str, Any]:
    rows = store.apify_contact_raw_rows(run_id=run_id, source=source, limit=limit)
    if not rows:
        return {
            "domains_processed": 0,
            "people_extracted": 0,
            "domains_with_person": 0,
            "rows_written": 0,
            "skipped_no_content": 0,
            "estimated_llm_cost_usd": 0.0,
            "run_id": run_id or None,
        }

    # One LLM call per domain (merge items).
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        d = (r.get("domain") or "").strip().lower()
        if not d:
            continue
        by_domain.setdefault(d, []).append(r)

    llm = make_llm(settings, model=model or "")


    domains_processed = 0
    people_extracted = 0
    domains_with_person = 0
    rows_written = 0
    skipped_no_content = 0
    company_rows: list[dict[str, Any]] = []
    contact_rows_with_email: list[dict[str, Any]] = []
    contact_rows_no_email: list[dict[str, Any]] = []

    def work(domain: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        texts = []
        source_url = ""
        for it in items:
            raw = it.get("raw_json") or "{}"
            try:
                payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            t = _item_text(payload)
            if t:
                texts.append(t)
            if not source_url:
                source_url = it.get("url") or _item_url(payload)
        page_text = "\n\n----\n\n".join(texts).strip()
        if not page_text:
            return {"domain": domain, "people": [], "skipped": True, "source_url": source_url}

        prompt = (
            f"DOMAIN: {domain}\nSOURCE_URL: {source_url}\n\n"
            f"CRAWL TEXT:\n---\n{page_text[:12000]}\n---\n\n"
            "Extract decision-maker people as JSON."
        )
        try:
            out = llm.json_chat(PARSE_SYSTEM, prompt, PARSE_SCHEMA)
        except (OllamaError, ValueError, TypeError, Exception):
            return {"domain": domain, "people": [], "skipped": False, "source_url": source_url}

        people = []
        for p in out.get("people") or []:
            if not isinstance(p, dict):
                continue
            first = str(p.get("first_name") or "").strip()
            last = str(p.get("last_name") or "").strip()
            if not _looks_like_person(first, last):
                continue
            email = str(p.get("email") or "").strip().lower()
            if email and not _email_on_page(email, page_text):
                email = ""  # drop invented emails
            try:
                conf = float(p.get("confidence") or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            people.append(
                {
                    "first_name": first,
                    "last_name": last,
                    "job_title": str(p.get("job_title") or "").strip(),
                    "email": email,
                    "phone": str(p.get("phone") or "").strip(),
                    "linkedin_url": str(p.get("linkedin_url") or "").strip(),
                    "confidence": max(0.0, min(1.0, conf)),
                    "source_url": source_url,
                }
            )
        return {
            "domain": domain,
            "people": people,
            "skipped": False,
            "source_url": source_url,
        }

    results = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers or 8))) as pool:
        futs = {
            pool.submit(work, d, items): d for d, items in by_domain.items()
        }
        for fut in as_completed(futs):
            results.append(fut.result())

    for res in results:
        domains_processed += 1
        if res.get("skipped"):
            skipped_no_content += 1
            company_rows.append(
                gc_sync.company_row(
                    domain=res["domain"],
                    dm_lookup_status="not_found",
                    dm_source_tier="apify_openai",
                    source_tier={"dm": "apify_openai"},
                )
            )
            continue
        people = res.get("people") or []
        people_extracted += len(people)
        if people:
            domains_with_person += 1
            company_rows.append(
                gc_sync.company_row(
                    domain=res["domain"],
                    dm_lookup_status="found",
                    dm_source_tier="apify_openai",
                    source_tier={"dm": "apify_openai"},
                )
            )
        else:
            company_rows.append(
                gc_sync.company_row(
                    domain=res["domain"],
                    dm_lookup_status="not_found",
                    dm_source_tier="apify_openai",
                    source_tier={"dm": "apify_openai"},
                )
            )
        for p in people:
            row = gc_sync.contact_row(
                domain=res["domain"],
                first_name=p["first_name"],
                last_name=p["last_name"],
                job_title=p.get("job_title") or "",
                email=p.get("email") or "",
                cellphone=p.get("phone") or "",
                linkedin_url=p.get("linkedin_url") or "",
                source_tool="apify:contact-info-scraper",
                source_tier="apify_openai",
                source_url=p.get("source_url") or res.get("source_url") or "",
                confidence=float(p.get("confidence") or 0.0),
            )
            if row.get("email"):
                contact_rows_with_email.append(row)
            else:
                contact_rows_no_email.append(row)
            # Local mirror
            store.save_contact(
                name=f"{p['first_name']} {p['last_name']}".strip(),
                domain=res["domain"],
                title=p.get("job_title") or "",
                email=p.get("email") or "",
                source="apify:contact-info-scraper",
                source_tier="apify_openai",
                confidence=float(p.get("confidence") or 0.0),
                source_url=p.get("source_url") or "",
            )

    if company_rows:
        gc_sync.upsert_companies(company_rows)
    written = 0
    if contact_rows_with_email:
        written += gc_sync.insert_contacts_ignore_conflict(contact_rows_with_email)
    if contact_rows_no_email:
        written += gc_sync.insert_contacts(contact_rows_no_email)
    rows_written = written

    # Rough LLM cost from spend meters when available
    llm_cost = 0.0
    spend = getattr(llm, "spend", None)
    if isinstance(spend, dict):
        # OpenAICompat tracks tokens; use settings prices if present
        pin = getattr(settings, "openai_price_in", 0.15)
        pout = getattr(settings, "openai_price_out", 0.60)
        # gpt-4o-mini defaults if using that model
        if "gpt-4o-mini" in (model or ""):
            pin, pout = 0.15, 0.60
        inn = float(spend.get("prompt_tokens") or spend.get("input_tokens") or 0)
        out = float(spend.get("output_tokens") or spend.get("completion_tokens") or 0)
        llm_cost = (inn * pin + out * pout) / 1_000_000.0
    elif hasattr(llm, "spend_line"):
        # best-effort parse "$x.xx" from spend line
        line = str(llm.spend_line())
        m = re.search(r"\$([0-9.]+)", line)
        if m:
            try:
                llm_cost = float(m.group(1))
            except ValueError:
                llm_cost = 0.0

    return {
        "domains_processed": domains_processed,
        "people_extracted": people_extracted,
        "domains_with_person": domains_with_person,
        "rows_written": rows_written,
        "skipped_no_content": skipped_no_content,
        "estimated_llm_cost_usd": round(llm_cost, 6),
        "run_id": run_id or None,
    }
