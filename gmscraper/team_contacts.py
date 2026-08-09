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
NAME_THEN_TITLE = re.compile(
    r"(?m)^[ \t]*(?P<name>[A-Z][a-z]+(?:[ \t]+[A-Za-z][a-z]+){1,2})[ \t]*\n"
    r"[ \t]*(?P<title>(?:Owner|Founder|Co-?Founder|President|CEO|COO|CFO|"
    r"Principal|Partner|Director|Vice[ \t]+President|VP|General[ \t]+Manager|"
    r"Manager|Superintendent|Project[ \t]+Manager)[^\n]{0,40})$",
    re.I,
)

JUNK_NAMES = {
    "about us", "our team", "meet the", "contact us", "learn more",
    "read more", "click here", "privacy policy", "terms of",
}

ENTITY_MARKERS = re.compile(
    r"\b(llc|inc\.?|corp\.?|ltd\.?|company|group|partners?|holdings?|"
    r"associates|corporation|construction|builders?|contractors?|services|"
    r"development)\b",
    re.I,
)
TITLE_ONLY = re.compile(
    r"^(project\s+manager|manager|president|ceo|owner|director|estimator|"
    r"superintendent|administrator|assistant|coordinator|engineer|"
    r"vice\s+president|general\s+manager)$",
    re.I,
)

DEFAULT_TARGET_TITLES = (
    "owner", "founder", "president", "principal", "partner", "chief",
    "vice president", "vp", "director", "head of", "general manager",
)

TEAM_SCHEMA = {
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

TEAM_SYSTEM = """You extract decision-maker people from company website page text.
Return ONLY valid JSON matching the schema. No prose, no markdown fences.

Rules:
1. Return a person only when a real human name is present. Never put a job title,
   department, or company name in first_name/last_name. If the page shows only
   "Project Manager" with no name, omit that entry.
2. Reject entity names. If the candidate contains LLC, Inc, Corp, Ltd, Company,
   Group, Partners, Holdings, Associates, or a similar organisation suffix,
   skip it.
3. Never invent or pattern-generate an email. Only return an address that appears
   verbatim in the page text. Do not construct first.last@domain.com.
4. Prefer senior and decision-making titles from the TARGET_TITLES list supplied
   in the user message. Skip field staff and general info inboxes.
5. Names in image alt text and figure captions count; team pages are often photo grids.
6. Set confidence between 0 and 1 for how clearly the name and title were stated.
7. Return {"people": []} when nothing qualifies. Empty is correct and expected.
"""

TEAM_PROMPT = """BUSINESS
Name: {name}
City: {city}, {state}
Website: {website}
Page URL: {url}
TARGET_TITLES: {titles}

PAGE TEXT:
---
{evidence}
---

Extract decision-maker people as JSON only.
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


def looks_like_person(first: str, last: str) -> bool:
    name = f"{first} {last}".strip()
    if len(name) < 3:
        return False
    if TITLE_ONLY.match(name.strip()):
        return False
    if ENTITY_MARKERS.search(name):
        return False
    if first.lower() in {"project", "general", "construction", "the", "our"}:
        return False
    if not re.search(r"[A-Za-z]{2,}", first):
        return False
    # Reject truncated surnames like "Steve W."
    last_alpha = re.sub(r"[^A-Za-z]", "", last or "")
    if last and len(last_alpha) < 2:
        return False
    return True


def _title_rank(title: str, target_titles: Sequence[str] | None = None) -> int:
    t = (title or "").lower()
    keys = list(target_titles) if target_titles else list(DEFAULT_TARGET_TITLES)
    for i, key in enumerate(keys):
        if key.lower() in t:
            return 100 - i
    return 0


def _email_on_page(email: str, page_text: str) -> bool:
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return False
    return e in (page_text or "").lower()


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
            parts = name.split()
            first, last = parts[0], parts[-1] if len(parts) > 1 else ""
            if not looks_like_person(first, last):
                continue
            key = name.lower()
            prev = found.get(key)
            conf = 0.55 if title else 0.4
            if not prev or conf > float(prev.get("confidence") or 0):
                found[key] = {
                    "name": name,
                    "first_name": first,
                    "last_name": last,
                    "title": title,
                    "email": "",
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
    target_titles: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    if not text or not llm:
        return []
    titles = list(target_titles) if target_titles else list(DEFAULT_TARGET_TITLES)
    try:
        out = llm.json_chat(
            TEAM_SYSTEM,
            TEAM_PROMPT.format(
                name=business.get("name") or "",
                city=business.get("city") or "",
                state=business.get("state") or "",
                website=business.get("website") or business.get("domain") or "",
                url=source_url or "",
                titles=", ".join(titles),
                evidence=condense(text, OWNER_HINTS, cap),
            ),
            TEAM_SCHEMA,
        )
    except (OllamaError, ValueError, TypeError):
        return []
    people = []
    for p in out.get("people") or []:
        if not isinstance(p, dict):
            continue
        first = str(p.get("first_name") or "").strip()
        last = str(p.get("last_name") or "").strip()
        if not looks_like_person(first, last):
            continue
        name = _clean_name(f"{first} {last}")
        if not name:
            continue
        email = str(p.get("email") or "").strip().lower()
        if email and not _email_on_page(email, text):
            email = ""
        title = _clean_title(str(p.get("job_title") or ""))
        try:
            conf = float(p.get("confidence") or 0.5)
        except (TypeError, ValueError):
            conf = 0.5
        people.append(
            {
                "name": name,
                "first_name": first,
                "last_name": last,
                "title": title,
                "email": email,
                "phone": str(p.get("phone") or "").strip(),
                "linkedin_url": str(p.get("linkedin_url") or "").strip(),
                "confidence": max(0.0, min(1.0, conf)),
                "source": "team_page_llm",
                "source_tier": "team_page_llm",
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
    target_titles: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    pages = _pages_for_domain(store, domain)
    people: dict[str, dict[str, Any]] = {}
    biz = business or {"domain": domain, "website": f"https://{domain}"}
    for page in pages:
        text = page.get("text") or ""
        url = page.get("url") or ""
        if use_llm and llm:
            extracted = extract_llm(
                llm,
                business=biz,
                text=text,
                source_url=url,
                target_titles=target_titles,
            )
        else:
            extracted = extract_heuristic(text, source_url=url)
        for p in extracted:
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
    use_llm: bool = True,
    llm: Ollama | None = None,
    domains: Sequence[str] | None = None,
    target_titles: Sequence[str] | None = None,
) -> dict[str, int]:
    """Extract contacts from team/about pages for fetched domains.

    Defaults to use_llm=True — heuristic extraction alone produces too many
    title/company false positives.
    """
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
            store,
            domain,
            llm=llm,
            use_llm=use_llm,
            business=biz,
            target_titles=target_titles,
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
                source=p.get("source") or "team_page",
                source_tier=p.get("source_tier") or "team_page",
                confidence=float(p.get("confidence") or 0.0),
                source_url=p.get("source_url") or "",
            ):
                saved += 1
        owners_updated = 0
        if people and place_ids:
            ranked = sorted(
                people,
                key=lambda x: (
                    -_title_rank(x.get("title") or "", target_titles),
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
