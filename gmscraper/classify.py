"""Stage 4: local Gemma confirms each business actually fits the ICP.

Google Maps categories are noisy -- searching "memorial park" returns public
parks, and "gym" returns equipment stores. This stage reads the business's own
website text and answers one question: is this the kind of company we want?

Runs on Ollama, so it costs nothing and there is no per-row rate limit beyond
your own hardware.
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Sequence

from .config import settings
from .evidence import ICP_HINTS, condense
from .llm import Ollama, OllamaError
from .store import Store

SYSTEM = (
    "You qualify local businesses for a B2B prospect list. You are given an "
    "ICP definition and evidence about one business. Decide whether the "
    "business matches the ICP. Judge only from the evidence: if the evidence "
    "is too thin to tell, say so with low confidence rather than guessing. "
    "Be strict about the ICP's exclusions."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "in_icp": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["in_icp", "confidence", "reason"],
}

PROMPT = """ICP:
{icp}

BUSINESS
Name: {name}
Google Maps category: {category}
All Maps categories: {types}
Address: {address}
Website: {website}

WEBSITE TEXT (homepage/about/team/contact, truncated):
---
{text}
---

Does this business match the ICP? Answer with JSON:
  in_icp     - true only if it clearly matches and hits none of the exclusions
  confidence - 0.0 to 1.0, how sure you are given the evidence
  reason     - one short sentence citing the evidence you used
"""

NO_SITE_NOTE = "(no website text available - judge from the Maps data alone)"


def run(
    store: Store,
    ollama: Ollama,
    icp: str,
    workers: int = 1,
    limit: int | None = None,
    include_no_site: bool = False,
    min_confidence: float = 0.0,
    max_evidence_chars: int | None = None,
) -> dict[str, int]:
    """Classify every business without a verdict yet."""
    where = "b.place_id NOT IN (SELECT place_id FROM verdicts)"
    if not include_no_site:
        where += (
            " AND b.domain IS NOT NULL AND b.domain != ''"
            " AND EXISTS (SELECT 1 FROM sites s WHERE s.domain=b.domain AND s.status='ok')"
        )
    sql = f"SELECT b.* FROM businesses b WHERE {where}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = list(store.conn.execute(sql))
    cap = max_evidence_chars or settings.max_evidence_chars

    if not rows:
        print("Nothing to classify.")
        return {"done": 0, "in_icp": 0, "errors": 0}

    print(
        f"Classifying {len(rows):,} businesses with {ollama.model} "
        f"({workers} worker{'s' if workers != 1 else ''}, {cap:,} chars evidence)"
    )
    counts = {"done": 0, "in_icp": 0, "errors": 0}
    lock = threading.Lock()

    def work(row) -> None:
        text = store.get_site_text(row["domain"]) if row["domain"] else None
        prompt = PROMPT.format(
            icp=icp.strip(),
            name=row["name"] or "",
            category=row["main_category"] or "",
            types=row["types"] or "[]",
            address=row["address"] or "",
            website=row["website"] or "(none)",
            text=condense(text, ICP_HINTS, cap) if text else NO_SITE_NOTE,
        )
        try:
            out = ollama.json_chat(SYSTEM, prompt, SCHEMA)
            in_icp = bool(out.get("in_icp"))
            conf = float(out.get("confidence") or 0.0)
            if conf < min_confidence:
                in_icp = False
            store.save_verdict(
                row["place_id"], in_icp, conf,
                str(out.get("reason") or "")[:500], ollama.model,
            )
            ok = True
        except (OllamaError, ValueError, TypeError) as exc:
            store.save_verdict(row["place_id"], None, 0.0, f"error: {exc}"[:500],
                               ollama.model)
            in_icp, ok = False, False

        with lock:
            counts["done"] += 1
            counts["in_icp"] += int(in_icp)
            counts["errors"] += int(not ok)
            if counts["done"] % 10 == 0 or counts["done"] == len(rows):
                sys.stderr.write(
                    f"\r  {counts['done']:,}/{len(rows):,} | "
                    f"in-ICP {counts['in_icp']:,} | errors {counts['errors']:,}   "
                )
                sys.stderr.flush()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, r) for r in rows]
        try:
            for f in as_completed(futures):
                f.exception()
        except KeyboardInterrupt:
            print("\nInterrupted -- verdicts so far are saved.")
            for f in futures:
                f.cancel()
    sys.stderr.write("\n")
    return counts
