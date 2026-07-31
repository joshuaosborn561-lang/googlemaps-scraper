"""Stage 5: pull the owner's name.

Same thing a teammate would do with "here's 100 businesses, find the owner":
read the company's own site first, and if that comes up empty, Google it.

    1. local Gemma reads the website text  -> source "website"  (free)
    2. if empty and --fallback is on, a Google SERP backend searches
       "who owns <business> in <city>" and Gemma reads that -> "websearch"

The model is told to return null rather than guess. A wrong first name in a
cold email is worse than no first name.
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import settings
from .evidence import OWNER_HINTS, condense
from .llm import Ollama, OllamaError
from .store import Store

SYSTEM = (
    "You extract the name of a local business's owner from evidence. Return "
    "the person who owns or founded the business -- not a generic manager, "
    "not the business name, not a reviewer or an author byline. If the "
    "evidence does not name an owner, return null. Never guess a name."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "owner_name": {"type": ["string", "null"]},
        "owner_title": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
    },
    "required": ["owner_name", "owner_title", "confidence"],
}

PROMPT = """BUSINESS
Name: {name}
City: {city}, {state}
Website: {website}

EVIDENCE ({source}):
---
{evidence}
---

Who owns this business? Answer with JSON:
  owner_name  - the owner's full name exactly as written, or null if not stated
  owner_title - their title if given (Owner, Founder, President, ...), else null
  confidence  - 0.0 to 1.0

Return null for owner_name unless the evidence actually names the owner,
founder, or principal of THIS business.
"""


def _ask(ollama: Ollama, row, evidence: str, source: str, cap: int = 2500):
    out = ollama.json_chat(
        SYSTEM,
        PROMPT.format(
            name=row["name"] or "",
            city=row["city"] or "",
            state=row["state"] or "",
            website=row["website"] or "(none)",
            source=source,
            evidence=condense(evidence, OWNER_HINTS, cap),
        ),
        SCHEMA,
    )
    name = out.get("owner_name")
    name = str(name).strip() if name else ""
    if name.lower() in {"null", "none", "n/a", "unknown", ""}:
        name = ""
    title = out.get("owner_title")
    title = str(title).strip() if title else ""
    try:
        conf = float(out.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return name, title, conf


def run(
    store: Store,
    ollama: Ollama,
    owj=None,
    workers: int = 1,
    limit: int | None = None,
    icp_only: bool = True,
    max_evidence_chars: int | None = None,
) -> dict[str, int]:
    where = "b.place_id NOT IN (SELECT place_id FROM owners)"
    if icp_only:
        where += (
            " AND EXISTS (SELECT 1 FROM verdicts v "
            "WHERE v.place_id=b.place_id AND v.in_icp=1)"
        )
    sql = f"SELECT b.* FROM businesses b WHERE {where}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = list(store.conn.execute(sql))
    cap = max_evidence_chars or settings.max_evidence_chars

    if not rows:
        print("Nothing to look up. (Run `classify` first, or pass --all.)")
        return {"done": 0, "found": 0, "via_web": 0}

    use_web = bool(owj and getattr(owj, "enabled", False))
    label = type(owj).__name__ if use_web else "off"
    print(
        f"Finding owners for {len(rows):,} businesses with {ollama.model} "
        f"({workers} worker{'s' if workers != 1 else ''}, web fallback {label})"
    )
    counts = {"done": 0, "found": 0, "via_web": 0, "errors": 0}
    lock = threading.Lock()

    def work(row) -> None:
        name = title = ""
        conf = 0.0
        source = "none"
        errored = False

        site_text = store.get_site_text(row["domain"]) if row["domain"] else None
        if site_text:
            try:
                name, title, conf = _ask(ollama, row, site_text, "the company website", cap)
                if name:
                    source = "website"
            except (OllamaError, ValueError, TypeError):
                errored = True

        if not name and use_web:
            city = row["city"] or row["state"] or ""
            query = f'who owns "{row["name"]}" in {city}'.strip()
            web_text = owj.search_text(query)  # type: ignore[union-attr]
            if web_text:
                try:
                    name, title, conf = _ask(ollama, row, web_text, "a web search", cap)
                    if name:
                        source = "websearch"
                except (OllamaError, ValueError, TypeError):
                    errored = True

        store.save_owner(row["place_id"], name or None, title or None, source,
                         conf, ollama.model)
        with lock:
            counts["done"] += 1
            counts["found"] += int(bool(name))
            counts["via_web"] += int(source == "websearch")
            counts["errors"] += int(errored)
            if counts["done"] % 10 == 0 or counts["done"] == len(rows):
                sys.stderr.write(
                    f"\r  {counts['done']:,}/{len(rows):,} | found "
                    f"{counts['found']:,} ({counts['via_web']:,} via web) | "
                    f"errors {counts['errors']:,}   "
                )
                sys.stderr.flush()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, r) for r in rows]
        try:
            for f in as_completed(futures):
                f.exception()
        except KeyboardInterrupt:
            print("\nInterrupted -- results so far are saved.")
            for f in futures:
                f.cancel()
    sys.stderr.write("\n")
    if use_web and owj.request_count:
        spend = owj.request_count * getattr(owj, "cost_per_search", 0.0)
        print(f"  web fallback: {owj.request_count:,} searches, ~${spend:,.2f}")
    return counts
