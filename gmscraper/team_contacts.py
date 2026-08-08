"""Extract person + title pairs from team/about page text into contacts."""

from __future__ import annotations

import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Sequence

from .evidence import OWNER_HINTS, condense
from .llm import Ollama, OllamaError
from .store import Store

# "Jane Doe, President" / "Jane Doe - Owner" (same line only)
NAME_TITLE_LINE = re.compile(
    r"(?m)^[ \t]*(?P<name>[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+|[ \t]+[A-Z]\.){1,3})"
    r"[ \t]*(?:,|-|–|:|\||\u2013|\u2014)[ \t]*"
    r"(?P<title>(?:Owner|Founder|Co-?Founder|President|CEO|COO|CFO|CTO|"
    r"Principal|Partner|Director|Managing[ \t]+Director|Vice[ \t]+President|VP|"
    r"General[ \t]+Manager|GM|Manager|Superintendent|Project[ \t]+Manager|"
    r"Operations[ \t]+Manager|Estimator|Controller)(?:[ \t]+[A-Za-z/]+){0,4})"
    r"[ \t]*$",
    re.I,
)
# "Jane Doe\nCEO"
NAME_THEN_TITLE = re.compile(
    r"(?m)^[ \t]*(?P<name>[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,2})[ \t]*\n"
    r"[ \t]*(?P<title>(?:Owner|Founder|Co-?Founder|President|CEO|COO|CFO|"
    r"Principal|Partner|Director|Vice[ \t]+President|VP|General[ \t]+Manager|"
    r"Manager|Superintendent|Project[ \t]+Manager)[^\n]{0,40})$",
    re.I,
)

JUNK_NAMES = {
    "about us", "our team", "meet the", "contact us", "learn more",
    "read more", "click here", "privacy policy", "terms of",
}

TEAM_SCHEMA = {
    "type": "object",
    "properties": {
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                },
                "required": ["name", "title", "confidence"],
            },
        }
    },
    "required": ["people"],
}

TEAM_SYSTEM = (
    "You extract people and their job titles from a company team/about page. "
    "Return only real people who work at THIS company. Skip reviewers, "
    "customers, city names, and navigation labels. Prefer owners, founders, "
    "principals, presidents, and senior leaders. Never invent names."
)

TEAM_PROMPT = """BUSINESS
Name: {name}
City: {city}, {state}
Website: {website}
Page URL: {url}

TEAM / ABOUT PAGE TEXT:
---
{evidence}
---

Return JSON with a "people" array. Each item:
  name       - full name as written, or null
  title      - job title if present, else null
  confidence - 0.0 to 1.0
"""


def _clean_name(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip())
    if len(n) < 3 or len(n) > 60:
        return ""
    if n.lower() in JUNK_NAMES:
        return ""
    if not re.search(r"[A-Za-z]", n):
        return ""
    return n


def _clean_title(title: str) -> str:
    t = re.sub(r"\s+", " ", (title or "").strip().strip("·•|-,"))
    if len(t) > 80:
        t = t[:80]
    return t


def _title_rank(title: str) -> int:
    t = (title or "").lower()
    for i, key in enumerate(
        (
            "owner", "founder", "principal", "president", "ceo", "partner",
            "director", "vp", "vice president", "manager",
        )
    ):
        if key in t:
            return 100 - i
    return 0


def extract_heuristic(text: str, source_url: str = "") -> list[dict[str, Any]]:
    """Regex person+title pairs from team/about text."""
    if not text:
        return []
    found: dict[str, dict[str, Any]] = {}
    for rx in (NAME_TITLE_LINE, NAME_THEN_TITLE):
        for m in rx.finditer(text):
            name = _clean_name(m.group("name"))
            title = _clean_title(m.group("title"))
            if not name or not title:
                continue
            key = name.lower()
            prev = found.get(key)
            conf = 0.55 if title else 0.4
            if not prev or conf > float(prev.get("confidence") or 0):
                found[key] = {
                    "name": name,
                    "title": title,
                    "confidence": conf,
                    "source": "team_page",
                    "source_tier": "team_page",
                    "source_url": source_url,
                }
    return list(found.values())


def extract_llm(
    llm: Ollama,
    *,
    business: dict[str, Any],
    text: str,
    source_url: str = "",
    cap: int = 2500,
) -> list[dict[str, Any]]:
    if not text or not llm:
        return []
    try:
        out = llm.json_chat(
            TEAM_SYSTEM,
            TEAM_PROMPT.format(
                name=business.get("name") or "",
                city=business.get("city") or "",
                state=business.get("state") or "",
                website=business.get("website") or business.get("domain") or "",
                url=source_url or "",
                evidence=condense(text, OWNER_HINTS, cap),
            ),
            TEAM_SCHEMA,
        )
    except (OllamaError, ValueError, TypeError):
        return []
    people = []
    for p in out.get("people") or []:
        name = _clean_name(str(p.get("name") or ""))
        if not name:
            continue
        title = _clean_title(str(p.get("title") or ""))
        try:
            conf = float(p.get("confidence") or 0.5)
        except (TypeError, ValueError):
            conf = 0.5
        people.append(
            {
                "name": name,
                "title": title,
                "confidence": conf,
                "source": "team_page",
                "source_tier": "team_page",
                "source_url": source_url,
            }
        )
    return people


def _pages_for_domain(store: Store, domain: str) -> list[dict[str, Any]]:
    pages = store.get_site_pages(domain, page_types=["team", "about"])
    if pages:
        return pages
    text = store.get_site_text(domain) or ""
    if not text:
        return []
    chunks: list[dict[str, Any]] = []
    parts = re.split(r"\[page_type=(\w+)\s+url=([^\]]+)\]\s*", text)
    if len(parts) >= 4:
        i = 1
        while i + 2 < len(parts):
            ptype, url, body = parts[i], parts[i + 1], parts[i + 2]
            if ptype in ("team", "about") and body.strip():
                chunks.append({"page_type": ptype, "url": url.strip(), "text": body})
            i += 3
    if chunks:
        return chunks
    return [{"page_type": "about", "url": f"https://{domain}/", "text": text}]


def _place_ids_for_domain(store: Store, domain: str) -> list[str]:
    return [
        r["place_id"]
        for r in store.conn.execute(
            "SELECT place_id FROM businesses WHERE domain=? ORDER BY place_id",
            (domain,),
        )
    ]


def extract_for_domain(
    store: Store,
    domain: str,
    *,
    llm: Ollama | None = None,
    use_llm: bool = False,
    business: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    pages = _pages_for_domain(store, domain)
    people: dict[str, dict[str, Any]] = {}
    biz = business or {"domain": domain, "website": f"https://{domain}"}
    for page in pages:
        text = page.get("text") or ""
        url = page.get("url") or ""
        for p in extract_heuristic(text, source_url=url):
            people[p["name"].lower()] = p
        if use_llm and llm:
            for p in extract_llm(llm, business=biz, text=text, source_url=url):
                key = p["name"].lower()
                if key not in people or float(p["confidence"]) > float(
                    people[key].get("confidence") or 0
                ):
                    people[key] = p
    return list(people.values())


def run(
    store: Store,
    *,
    limit: int | None = None,
    workers: int = 8,
    icp_only: bool = False,
    use_llm: bool = False,
    llm: Ollama | None = None,
    domains: Sequence[str] | None = None,
) -> dict[str, int]:
    """Extract contacts from team/about pages for fetched domains."""
    if domains is None:
        if icp_only:
            sql = """
                SELECT DISTINCT b.domain FROM businesses b
                JOIN verdicts v ON v.place_id=b.place_id AND v.in_icp=1
                JOIN sites s ON s.domain=b.domain AND s.status='ok'
                WHERE b.domain IS NOT NULL AND b.domain != ''
                ORDER BY b.domain
            """
            domains = [r["domain"] for r in store.conn.execute(sql)]
        else:
            domains = store.domains_with_ok_sites(limit=None)
    domains = list(domains)
    if limit:
        domains = domains[: int(limit)]

    if not domains:
        return {"domains": 0, "contacts": 0, "owners_updated": 0}

    print(
        f"Extracting team contacts from {len(domains):,} domains "
        f"(llm={'on' if use_llm and llm else 'off'}, {workers} workers)"
    )
    counts = {"domains": 0, "contacts": 0, "owners_updated": 0}
    lock = threading.Lock()

    def work(domain: str) -> None:
        biz_row = store.conn.execute(
            "SELECT * FROM businesses WHERE domain=? LIMIT 1", (domain,)
        ).fetchone()
        biz = dict(biz_row) if biz_row else {"domain": domain}
        people = extract_for_domain(
            store, domain, llm=llm, use_llm=use_llm, business=biz
        )
        place_ids = _place_ids_for_domain(store, domain)
        saved = 0
        for p in people:
            if store.save_contact(
                name=p["name"],
                domain=domain,
                place_id=place_ids[0] if place_ids else "",
                title=p.get("title") or "",
                email=p.get("email") or "",
                source="team_page",
                source_tier="team_page",
                confidence=float(p.get("confidence") or 0.0),
                source_url=p.get("source_url") or "",
            ):
                saved += 1
        owners_updated = 0
        if people and place_ids:
            ranked = sorted(
                people,
                key=lambda x: (
                    -_title_rank(x.get("title") or ""),
                    -float(x.get("confidence") or 0),
                ),
            )
            best = ranked[0]
            model_name = (
                getattr(llm, "model", "llm") if (use_llm and llm) else "heuristic"
            )
            for pid in place_ids:
                existing = store.conn.execute(
                    "SELECT owner_name FROM owners WHERE place_id=?", (pid,)
                ).fetchone()
                if existing and (existing["owner_name"] or "").strip():
                    continue
                store.save_owner(
                    pid,
                    best["name"],
                    best.get("title") or None,
                    "team_page",
                    float(best.get("confidence") or 0.5),
                    model_name,
                )
                owners_updated += 1
        with lock:
            counts["domains"] += 1
            counts["contacts"] += saved
            counts["owners_updated"] += owners_updated
            if counts["domains"] % 50 == 0 or counts["domains"] == len(domains):
                sys.stderr.write(
                    f"\r  team-extract {counts['domains']:,}/{len(domains):,} | "
                    f"contacts={counts['contacts']:,} "
                    f"owners={counts['owners_updated']:,}   "
                )
                sys.stderr.flush()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, d) for d in domains]
        for f in as_completed(futures):
            f.exception()
    sys.stderr.write("\n")
    return counts
