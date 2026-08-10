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
from typing import Any, Callable, Sequence

from .config import settings
from .evidence import ICP_HINTS, condense
from .llm import Ollama, OllamaError, RateLimitError, is_rate_limit_error
from .store import Store
from .zips import haversine_miles, parse_center

SYSTEM = (
    "You answer simple yes/no ICP checks for local businesses. "
    "Read the ICP questions, glance at the Maps category and website text, "
    "and answer quickly. Do not reason at length. Do not invent facts. "
    "Only reject what is obviously out. When unsure, answer yes with lower "
    "confidence rather than over-filtering."
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

PROMPT = """ICP (simple yes/no checks):
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

Answer with JSON only:
  in_icp     - true unless the business is obviously not an ICP match
  confidence - 0.0 to 1.0
  reason     - one short phrase (not a paragraph)
"""

NO_SITE_NOTE = "(no website text available - judge from the Maps data alone)"


def _eligible_clauses(
    *,
    source: str = "",
    force: bool = False,
    include_no_site: bool = False,
    city: str = "",
    state: str = "",
    main_category: str = "",
    plan_id: str = "",
    run_id: str = "",
    client_tag: str = "",
) -> tuple[str, list]:
    clauses: list[str] = []
    args: list = []
    if not force:
        # Retry rate-limit failures — those must not permanently burn a row.
        clauses.append(
            """(
              b.place_id NOT IN (SELECT place_id FROM verdicts)
              OR b.place_id IN (
                SELECT place_id FROM verdicts
                WHERE reason LIKE 'error:%429%'
                   OR reason LIKE 'error:%rate limit%'
                   OR reason LIKE 'error:%RateLimit%'
              )
            )"""
        )
    if source:
        clauses.append("COALESCE(NULLIF(b.source,''), 'maps') = ?")
        args.append(source.strip().lower())
    if city.strip():
        clauses.append("lower(b.city) = lower(?)")
        args.append(city.strip())
    if state.strip():
        states = [s.strip().upper() for s in state.split(",") if s.strip()]
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"upper(COALESCE(b.state,'')) IN ({placeholders})")
            args.extend(states)
    if main_category.strip():
        cats = [c.strip() for c in main_category.split(",") if c.strip()]
        if cats:
            ors = " OR ".join(
                "lower(COALESCE(b.main_category,'')) LIKE ?" for _ in cats
            )
            clauses.append(f"({ors})")
            args.extend(f"%{c.lower()}%" for c in cats)
    if plan_id.strip():
        clauses.append("b.plan_id = ?")
        args.append(plan_id.strip())
    if run_id.strip():
        clauses.append("b.run_id = ?")
        args.append(run_id.strip())
    if client_tag.strip():
        clauses.append("b.client_tag = ?")
        args.append(client_tag.strip())
    if not include_no_site:
        clauses.append("b.domain IS NOT NULL AND b.domain != ''")
        clauses.append(
            "EXISTS (SELECT 1 FROM sites s WHERE s.domain=b.domain AND s.status='ok')"
        )
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, args


def _geo_center(
    center: str = "",
    center_lat: float | None = None,
    center_lng: float | None = None,
) -> tuple[float, float] | None:
    if center_lat is not None and center_lng is not None:
        return float(center_lat), float(center_lng)
    text = (center or "").strip()
    if not text:
        return None
    try:
        lat, lng, _label = parse_center(text)
        return float(lat), float(lng)
    except Exception:  # noqa: BLE001
        return None


def _within_radius(
    row: Any,
    *,
    lat: float,
    lng: float,
    radius_miles: float,
) -> bool:
    try:
        rlat = row["latitude"] if "latitude" in row.keys() else None
        rlng = row["longitude"] if "longitude" in row.keys() else None
    except Exception:  # noqa: BLE001
        rlat = row.get("latitude") if hasattr(row, "get") else None
        rlng = row.get("longitude") if hasattr(row, "get") else None
    if rlat is None or rlng is None:
        return False
    try:
        return haversine_miles(float(lat), float(lng), float(rlat), float(rlng)) <= float(
            radius_miles
        )
    except (TypeError, ValueError):
        return False


def run(
    store: Store,
    ollama: Ollama,
    icp: str,
    workers: int = 1,
    limit: int | None = None,
    include_no_site: bool = False,
    min_confidence: float = 0.0,
    max_evidence_chars: int | None = None,
    source: str = "",
    force: bool = False,
    center: str = "",
    radius_miles: float = 0.0,
    center_lat: float | None = None,
    center_lng: float | None = None,
    require_geo: bool = False,
    city: str = "",
    state: str = "",
    main_category: str = "",
    plan_id: str = "",
    run_id: str = "",
    client_tag: str = "",
    on_progress: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Classify businesses against an ICP.

    By default only unclassified rows with fetched site text are eligible.
    Pass source= to scope (e.g. 'shovels'), force=True to re-classify, and
    limit= to cap the batch. Scope further with city/state/main_category/
    plan_id/run_id/client_tag so one client's rows can be classified without
    draining another client's backlog.

    When require_geo=True (or center+radius_miles are set), rows outside the
    radius are rejected deterministically before any LLM call and saved as
    in_icp=false with reason 'outside_radius'.
    """
    from .llm import OpenAICompat, llm_max_concurrency

    workers = max(1, int(workers or 1))
    if isinstance(ollama, OpenAICompat):
        # Extra threads beyond the shared gate only stampede retries.
        workers = min(workers, llm_max_concurrency())

    where, args = _eligible_clauses(
        source=source,
        force=force,
        include_no_site=include_no_site,
        city=city,
        state=state,
        main_category=main_category,
        plan_id=plan_id,
        run_id=run_id,
        client_tag=client_tag,
    )
    # Count full eligible set before LIMIT so callers get has_more.
    total_eligible = store.conn.execute(
        f"SELECT COUNT(*) FROM businesses b{where}", args
    ).fetchone()[0]
    sql = f"SELECT b.* FROM businesses b{where} ORDER BY b.first_seen DESC, b.place_id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = list(store.conn.execute(sql, args))
    cap = max_evidence_chars or settings.max_evidence_chars

    geo = None
    use_geo = bool(require_geo) or (
        float(radius_miles or 0) > 0
        and bool(center or (center_lat is not None and center_lng is not None))
    )
    if use_geo:
        if float(radius_miles or 0) <= 0:
            return {
                "done": 0,
                "in_icp": 0,
                "errors": 0,
                "reason": "require_geo/radius set but radius_miles is missing or <= 0",
                "geo_rejected": 0,
            }
        geo = _geo_center(center, center_lat, center_lng)
        if geo is None:
            return {
                "done": 0,
                "in_icp": 0,
                "errors": 0,
                "reason": "require_geo set but center could not be resolved to lat/lng",
                "geo_rejected": 0,
            }

    if not rows:
        print("Nothing to classify.")
        stats = store.stats()
        pending_where, pending_args = _eligible_clauses(
            source=source, force=False, include_no_site=include_no_site
        )
        pending_eligible = store.conn.execute(
            f"SELECT COUNT(*) FROM businesses b{pending_where}", pending_args
        ).fetchone()[0]
        # Source-scoped no-site count.
        no_site_clauses = ["(b.domain IS NULL OR b.domain = '' OR NOT EXISTS (SELECT 1 FROM sites s WHERE s.domain=b.domain AND s.status='ok'))"]
        no_site_args: list = []
        if source:
            no_site_clauses.append("COALESCE(NULLIF(b.source,''), 'maps') = ?")
            no_site_args.append(source.strip().lower())
        no_site = store.conn.execute(
            "SELECT COUNT(*) FROM businesses b WHERE " + " AND ".join(no_site_clauses),
            no_site_args,
        ).fetchone()[0]
        already_q = "SELECT COUNT(*) FROM verdicts v JOIN businesses b ON b.place_id=v.place_id"
        already_args: list = []
        if source:
            already_q += " WHERE COALESCE(NULLIF(b.source,''), 'maps') = ?"
            already_args.append(source.strip().lower())
        already = store.conn.execute(already_q, already_args).fetchone()[0]
        src_note = f" for source={source!r}" if source else ""
        if already and not pending_eligible and not force:
            reason = (
                f"nothing eligible{src_note}: all classifiable businesses already have verdicts "
                f"({already:,} classified; {no_site:,} businesses have no site text). "
                f"Pass force=true to re-classify, or resolve_domains / enrich_sites first."
            )
        elif no_site and not include_no_site:
            reason = (
                f"nothing eligible{src_note}: {no_site:,} businesses have no site text"
                + (f"; {already:,} already classified" if already else "")
                + ". Call estimate_resolve_domains / resolve_domains then enrich_sites."
            )
        else:
            reason = f"nothing eligible{src_note}: no businesses match the classify filters"
        return {
            "done": 0,
            "in_icp": 0,
            "errors": 0,
            "reason": reason,
            "source": source or None,
            "force": force,
            "unclassifiable_no_site": no_site,
            "classified": already,
            "classifiable_with_site": int(stats.get("classifiable_with_site") or 0),
            "total_eligible": int(total_eligible),
            "remaining": int(total_eligible),
            "has_more": int(total_eligible) > 0,
            "processed": 0,
        }

    if force:
        for row in rows:
            store.clear_verdict(row["place_id"])

    geo_rejected = 0
    llm_rows = rows
    if geo is not None:
        inside: list = []
        for row in rows:
            if _within_radius(
                row, lat=geo[0], lng=geo[1], radius_miles=float(radius_miles)
            ):
                inside.append(row)
            else:
                store.save_verdict(
                    row["place_id"],
                    False,
                    1.0,
                    f"outside_radius:{radius_miles:g}mi",
                    "geo_gate",
                )
                geo_rejected += 1
        llm_rows = inside

    scope = f" source={source!r}" if source else ""
    print(
        f"Classifying {len(llm_rows):,} businesses{scope} with {ollama.model} "
        f"({workers} worker{'s' if workers != 1 else ''}, {cap:,} chars evidence"
        f"{', force' if force else ''}"
        f"{f', geo_rejected={geo_rejected}' if geo_rejected else ''})"
    )
    # Drop stale rate-limit burns so they become eligible again this run.
    cur = store.conn.execute(
        """DELETE FROM verdicts
           WHERE reason LIKE 'error:%429%'
              OR reason LIKE 'error:%rate limit%'
              OR reason LIKE 'error:%RateLimit%'"""
    )
    cleared_429 = int(cur.rowcount or 0)
    try:
        store.conn.commit()
    except Exception:  # noqa: BLE001
        pass
    if cleared_429:
        print(f"Cleared {cleared_429:,} rate-limit error verdicts for retry.")

    counts: dict[str, Any] = {
        "done": 0,
        "in_icp": 0,
        "errors": 0,
        "rate_limited": 0,
        "geo_rejected": geo_rejected,
        "cleared_rate_limit_verdicts": int(cleared_429 or 0),
        "source": source or None,
        "force": force,
        "require_geo": bool(use_geo),
        "radius_miles": float(radius_miles) if use_geo else None,
        "center_lat": geo[0] if geo else None,
        "center_lng": geo[1] if geo else None,
    }
    lock = threading.Lock()
    stop = threading.Event()

    def _cancelled() -> bool:
        if stop.is_set():
            return True
        try:
            from mcp_server.jobs import is_cancel_requested

            return bool(is_cancel_requested())
        except Exception:  # noqa: BLE001
            return False

    def work(row) -> None:
        if _cancelled():
            return
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
        in_icp = False
        ok = False
        rate_limited = False
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
        except RateLimitError:
            # Leave no permanent verdict — row stays eligible.
            rate_limited = True
            stop.set()  # stop the stampede; resume later
        except (OllamaError, ValueError, TypeError) as exc:
            if is_rate_limit_error(exc):
                rate_limited = True
                stop.set()
            else:
                store.save_verdict(
                    row["place_id"], None, 0.0, f"error: {exc}"[:500],
                    ollama.model,
                )

        with lock:
            counts["done"] += 1
            counts["in_icp"] += int(in_icp)
            counts["errors"] += int(not ok and not rate_limited)
            counts["rate_limited"] += int(rate_limited)
            done_n = int(counts["done"])
            in_icp_n = int(counts["in_icp"])
            err_n = int(counts["errors"])
            rl_n = int(counts["rate_limited"])
            if done_n % 10 == 0 or done_n == len(llm_rows):
                sys.stderr.write(
                    f"\r  {done_n:,}/{len(llm_rows):,} | "
                    f"in-ICP {in_icp_n:,} | errors {err_n:,} | 429 {rl_n:,}   "
                )
                sys.stderr.flush()
                if on_progress is not None:
                    try:
                        on_progress(
                            stage="classify",
                            done=done_n,
                            total=len(llm_rows),
                            jobs_done=done_n,
                            jobs_total=len(llm_rows),
                            jobs_pending=max(0, len(llm_rows) - done_n),
                            businesses_found=in_icp_n,
                            in_icp=in_icp_n,
                            errors=err_n,
                            rate_limited=rl_n,
                            geo_rejected=geo_rejected,
                        )
                    except Exception:  # noqa: BLE001
                        pass

    if not llm_rows:
        counts["done"] = geo_rejected
        counts["processed"] = geo_rejected
        counts["reason"] = (
            f"all {geo_rejected} eligible rows were outside the geo radius"
            if geo_rejected
            else "nothing to classify after filters"
        )
        remaining = max(0, int(total_eligible) - int(counts["processed"]))
        counts["total_eligible"] = int(total_eligible)
        counts["remaining"] = remaining
        counts["has_more"] = remaining > 0
        return counts

    if on_progress is not None:
        try:
            on_progress(
                stage="classify",
                done=0,
                total=len(llm_rows),
                jobs_done=0,
                jobs_total=len(llm_rows),
                jobs_pending=len(llm_rows),
                businesses_found=0,
                in_icp=0,
                errors=0,
                geo_rejected=geo_rejected,
            )
        except Exception:  # noqa: BLE001
            pass

    # Submit in chunks so cancel_job / rate-limit stop can take effect.
    chunk = max(workers * 2, 16)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            for i in range(0, len(llm_rows), chunk):
                if _cancelled():
                    counts["cancelled"] = True
                    break
                batch = llm_rows[i : i + chunk]
                futures = [pool.submit(work, r) for r in batch]
                for f in as_completed(futures):
                    f.exception()
                if stop.is_set():
                    counts["paused_rate_limit"] = True
                    print(
                        "\nPausing classify: OpenAI rate limit. "
                        "Rows were not permanently burned; re-run to continue."
                    )
                    break
        except KeyboardInterrupt:
            print("\nInterrupted -- verdicts so far are saved.")
            stop.set()
    sys.stderr.write("\n")
    counts["done"] = int(counts["done"]) + geo_rejected
    counts["processed"] = int(counts["done"])
    remaining = max(0, int(total_eligible) - int(counts["processed"]))
    counts["total_eligible"] = int(total_eligible)
    counts["remaining"] = remaining
    counts["has_more"] = remaining > 0 or bool(counts.get("paused_rate_limit"))
    return counts
