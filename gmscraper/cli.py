"""Command line entry point: python -m gmscraper <command>"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import (
    __version__,
    bench,
    brief as brief_mod,
    classify,
    enrich_site,
    export,
    mapsdata,
    owner,
    scrape,
    zips,
)
from .config import DEFAULT_CATEGORIES, DEFAULT_DB, DEFAULT_ZIPS, settings
from .llm import Ollama
from .mapsdata import MapsDataClient
from .websearch import make_backend
from .store import Store


# --------------------------------------------------------------- helpers


def load_verticals(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"categories file not found: {p}")
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit("pip install -r requirements.txt (missing PyYAML)") from exc
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{p} must be a mapping of vertical -> {{icp, categories}}")
    return data


def pick_vertical(path: str | Path, name: str) -> tuple[str, list[str]]:
    data = load_verticals(path)
    if name not in data:
        raise SystemExit(
            f"vertical '{name}' not in {path}. Available: {', '.join(sorted(data))}"
        )
    block = data[name] or {}
    cats = [str(c).strip() for c in (block.get("categories") or []) if str(c).strip()]
    if not cats:
        raise SystemExit(f"vertical '{name}' has no categories")
    return str(block.get("icp") or "").strip(), cats


def resolve_categories(args) -> tuple[str, list[str]]:
    """--categories wins over --vertical; --icp overrides the vertical's ICP."""
    if getattr(args, "categories", None):
        cats = [c.strip() for c in args.categories.split(",") if c.strip()]
        return (getattr(args, "icp", "") or "").strip(), cats
    icp, cats = pick_vertical(args.categories_file, args.vertical)
    if getattr(args, "icp", ""):
        icp = args.icp.strip()
    return icp, cats


def make_store(args) -> Store:
    return Store(args.db)


def make_ollama(args) -> Ollama:
    o = Ollama(
        host=args.ollama_host or settings.ollama_host,
        model=args.model or settings.ollama_model,
        num_ctx=args.num_ctx or settings.ollama_num_ctx,
        timeout=settings.ollama_timeout,
    )
    o.check()
    return o


# -------------------------------------------------------------- commands


def cmd_zips(args) -> None:
    n = zips.build(
        args.out,
        types=[t.strip() for t in args.types.split(",")],
        include_territories=args.territories,
        active_only=not args.include_inactive,
    )
    print(f"Wrote {n:,} ZIP codes -> {args.out}")


def cmd_categories(args) -> None:
    data = load_verticals(args.categories_file)
    for name, block in sorted(data.items()):
        cats = (block or {}).get("categories") or []
        print(f"\n{name}  ({len(cats)} categories)")
        icp = " ".join(str((block or {}).get("icp", "")).split())
        if icp:
            print(f"  ICP: {icp}")
        print(f"  {', '.join(cats)}")
    print()


def cmd_estimate(args) -> None:
    _, cats = resolve_categories(args)
    rows = zips.load(args.zips, states=args.states, limit=args.limit)
    n = len(rows) * len(cats)
    used = make_store(args).requests_this_cycle(settings.quota_reset_day)
    print(f"ZIP codes:   {len(rows):,}")
    print(f"Categories:  {len(cats)}")
    print(f"Requests:    {n:,}")
    print(f"Max rows:    {n * args.limit_results:,} before dedup")
    for line in brief_mod.cost_lines(n, settings.plan, used):
        print(line)
    for w in (4, 8, 16):
        print(f"  ~{n / (w * 3) / 3600:,.1f}h at {w} workers (assumes ~3 req/s/worker)")


def cmd_probe(args) -> None:
    """One live request, printed raw. Run this before any big scrape."""
    settings.require_rapidapi()
    rows = zips.load(args.zips, limit=None)
    row = next((r for r in rows if r["zip"] == args.zip), None) if args.zip else rows[0]
    if row is None:
        raise SystemExit(f"zip {args.zip} not in {args.zips}")

    client = MapsDataClient(settings, limit=args.limit_results,
                            query_template=args.query_template)
    print(f"GET {settings.maps_url}")
    print(f"params: {json.dumps(client.build_params(args.category, row), indent=2)}\n")

    payload = client.raw_search(args.category, row)
    items = mapsdata.extract_list(payload)
    print(f"--- raw response (first item of {len(items)}) ---")
    print(json.dumps(items[0] if items else payload, indent=2)[:4000])

    if not items:
        print("\nNo result list found. Check MAPS_DATA_PATH and the params above.")
        return

    norm = mapsdata.normalize(items[0], row["zip"], args.category)
    norm.pop("raw", None)
    print("\n--- normalized ---")
    print(json.dumps(norm, indent=2))
    empty = [k for k, v in norm.items() if v in ("", None, [])]
    if empty:
        print(
            f"\nEmpty after mapping: {', '.join(empty)}\n"
            f"If those exist in the raw item above under another name, add that "
            f"name to ALIASES in gmscraper/mapsdata.py and re-run probe."
        )


def cmd_scrape(args) -> None:
    settings.require_rapidapi()
    _, cats = resolve_categories(args)
    zip_rows = zips.load(args.zips, states=args.states, limit=args.limit)
    store = make_store(args)
    client = MapsDataClient(
        settings,
        limit=args.limit_results,
        query_template=args.query_template,
        extra_params=dict(p.split("=", 1) for p in args.param),
    )
    res = scrape.run(
        store, client, zip_rows, cats,
        workers=args.workers,
        price_per_request=settings.price_per_request,
        max_jobs=args.max_jobs,
    )
    print(
        f"\n{res['done']:,} searches, {res['new']:,} new businesses, "
        f"{res['errors']:,} errors. Total in db: {store.stats()['businesses']:,}"
    )


def cmd_enrich(args) -> None:
    store = make_store(args)
    store.queue_sites()
    domains = store.pending_sites(limit=args.limit)
    res = enrich_site.run(
        store, domains,
        workers=args.workers,
        timeout=args.timeout,
        respect_robots=not args.ignore_robots,
        delay=args.delay,
    )
    print(f"ok={res['ok']:,} error={res['error']:,} skipped={res['skipped']:,}")


def cmd_classify(args) -> None:
    icp, _ = resolve_categories(args)
    if not icp:
        raise SystemExit("No ICP text. Pass --icp or use a --vertical that defines one.")
    store = make_store(args)
    res = classify.run(
        store, make_ollama(args), icp,
        workers=args.workers,
        limit=args.limit,
        include_no_site=args.include_no_site,
        min_confidence=args.min_confidence,
        max_evidence_chars=args.evidence_chars or None,
    )
    print(f"classified={res['done']:,} in-ICP={res['in_icp']:,} errors={res['errors']:,}")


def cmd_owners(args) -> None:
    store = make_store(args)
    owj = make_backend(settings, args.fallback_source) if args.fallback else None
    if args.fallback and not (owj and owj.enabled):
        print(
            "--fallback requested but no credentials for "
            f"'{args.fallback_source or settings.fallback_source}'. "
            "Set APIFY_TOKEN in .env (or FALLBACK_SOURCE=openwebninja). "
            "Running website-only."
        )
    res = owner.run(
        store, make_ollama(args), owj,
        workers=args.workers, limit=args.limit, icp_only=not args.all,
        max_evidence_chars=args.evidence_chars or None,
    )
    print(f"done={res['done']:,} found={res['found']:,} via_web={res['via_web']:,}")


def cmd_export(args) -> None:
    store = make_store(args)
    n = export.run(
        store, args.out,
        icp_only=not args.all,
        with_owner=args.with_owner,
        with_phone=args.with_phone,
        with_website=args.with_website,
        with_email=args.with_email,
        min_confidence=args.min_confidence,
        min_rating=args.min_rating,
        min_reviews=args.min_reviews,
        states=args.states,
    )
    print(f"Wrote {n:,} rows -> {args.out}")


def _build_plan(args):
    """Plan from a --plan file, or by asking the local model about the brief."""
    if getattr(args, "plan", ""):
        return brief_mod.load(args.plan)
    text = " ".join(args.brief).strip()
    if not text:
        raise SystemExit('Describe what you want, e.g. gmscraper plan "HVAC in Ohio"')
    print(f'Planning: "{text}"\n(asking {args.model or settings.ollama_model}...)\n')
    return brief_mod.make_plan(make_ollama(args), text)


def cmd_plan(args) -> None:
    plan = _build_plan(args)
    zip_rows = zips.load(args.zips, states=plan.states or None, limit=args.limit)
    used = make_store(args).requests_this_cycle(settings.quota_reset_day)
    print("PLAN")
    print(plan.describe(len(zip_rows), settings.plan, used))
    if args.save:
        brief_mod.save(plan, args.save)
        print(f"\nSaved -> {args.save}")
        print(f"Edit it, then: python -m gmscraper run --plan {args.save}")
    else:
        print("\nAdd to config/categories.yml to keep it:\n")
        print(plan.to_yaml_block())


def cmd_run(args) -> None:
    """Plan, confirm, then drive every stage to a finished CSV."""
    settings.require_rapidapi()
    ollama = make_ollama(args)          # fail now, not after the scrape
    plan = _build_plan(args)
    zip_rows = zips.load(args.zips, states=plan.states or None, limit=args.limit)

    store = make_store(args)
    print("PLAN")
    print(plan.describe(len(zip_rows), settings.plan, store.requests_this_cycle(settings.quota_reset_day)))
    if not args.yes:
        try:
            if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
                raise SystemExit("Cancelled. Nothing spent.")
        except EOFError:
            raise SystemExit("Cancelled (no tty). Re-run with --yes.") from None

    client = MapsDataClient(settings, limit=args.limit_results,
                            query_template=args.query_template)

    print("\n[1/5] scraping Google Maps")
    scrape.run(store, client, zip_rows, plan.categories, workers=args.workers,
               price_per_request=settings.price_per_request)

    print("\n[2/5] fetching websites")
    store.queue_sites()
    enrich_site.run(store, store.pending_sites(), workers=args.site_workers,
                    respect_robots=not args.ignore_robots)

    print("\n[3/5] qualifying against the ICP")
    classify.run(store, ollama, plan.icp, workers=args.llm_workers,
                 min_confidence=args.min_confidence)

    if plan.require_owner or args.owners:
        print("\n[4/5] finding owner names")
        owj = make_backend(settings, args.fallback_source) if args.fallback else None
        owner.run(store, ollama, owj, workers=args.llm_workers)
    else:
        print("\n[4/5] skipping owner lookup (not requested; --owners to force)")

    print("\n[5/5] exporting")
    n = export.run(
        store, args.out,
        icp_only=True,
        with_owner=plan.require_owner,
        with_phone=plan.require_phone,
        with_website=plan.require_website,
        with_email=plan.require_email,
        min_confidence=args.min_confidence,
        min_rating=plan.min_rating,
        min_reviews=plan.min_reviews,
        states=plan.states or None,
    )
    print(f"\nDone. {n:,} leads -> {args.out}")
    cmd_stats(args)


def cmd_bench(args) -> None:
    models = (
        [m.strip() for m in args.models.split(",") if m.strip()]
        if args.models else bench.SUGGESTED
    )
    bench.run(
        args.ollama_host or settings.ollama_host,
        models,
        evidence_chars=args.evidence_chars,
        n_businesses=args.rows,
        runs=args.runs,
    )


def cmd_stats(args) -> None:
    s = make_store(args).stats()
    width = max(len(k) for k in s)
    for k, v in s.items():
        print(f"  {k.replace('_', ' '):<{width}}  {v:>12,}")


def cmd_renormalize(args) -> None:
    """Re-map stored raw JSON after editing ALIASES. No API calls."""
    store = make_store(args)
    rows = list(store.conn.execute("SELECT place_id, raw_json, source_zip, source_category FROM businesses"))
    updated = 0
    for r in rows:
        try:
            raw = json.loads(r["raw_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not raw:
            continue
        rec = mapsdata.normalize(raw, r["source_zip"] or "", r["source_category"] or "")
        with store.conn as c:
            c.execute(
                """UPDATE businesses SET name=?, address=?, city=?, state=?, zip=?,
                   phone=?, website=?, domain=?, rating=?, reviews=?,
                   main_category=?, types=?, latitude=?, longitude=?, maps_url=?
                   WHERE place_id=?""",
                (rec["name"], rec["address"], rec["city"], rec["state"], rec["zip"],
                 rec["phone"], rec["website"], rec["domain"], rec["rating"],
                 rec["reviews"], rec["main_category"], json.dumps(rec["types"]),
                 rec["latitude"], rec["longitude"], rec["maps_url"], r["place_id"]),
            )
        updated += 1
    print(f"Re-normalized {updated:,} rows from stored raw JSON (0 API calls).")


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gmscraper",
        description="Scrape US local businesses off Google Maps and qualify "
                    "them with a local LLM.",
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite state file")
    sub = p.add_subparsers(dest="command", required=True)

    def add_cat_args(sp, need_icp: bool = False) -> None:
        sp.add_argument("--vertical", default="funeral", help="block in categories.yml")
        sp.add_argument("--categories", help="comma-separated list, overrides --vertical")
        sp.add_argument("--categories-file", default=str(DEFAULT_CATEGORIES))
        if need_icp:
            sp.add_argument("--icp", default="", help="override the vertical's ICP text")

    def add_zip_args(sp) -> None:
        sp.add_argument("--zips", default=str(DEFAULT_ZIPS))
        sp.add_argument("--states", nargs="*", help="restrict to these state codes")
        sp.add_argument("--limit", type=int, help="use only the first N zips")

    def add_llm_args(sp) -> None:
        sp.add_argument("--model", default="", help=f"default: {settings.ollama_model}")
        sp.add_argument("--ollama-host", default="")
        sp.add_argument("--num-ctx", type=int, default=0,
                        help="0 = use OLLAMA_NUM_CTX from .env")

    sp = sub.add_parser(
        "plan", help='turn a plain-English brief into a run plan (free, no scraping)'
    )
    sp.add_argument("brief", nargs="*", help='e.g. "HVAC companies in Ohio, 4+ stars"')
    sp.add_argument("--plan", default="", help="load a saved plan instead")
    sp.add_argument("--save", default="", help="write the plan to this JSON file")
    add_zip_args(sp)
    add_llm_args(sp)
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("run", help="plan, confirm, then run every stage to a CSV")
    sp.add_argument("brief", nargs="*", help='e.g. "gyms in TX with 50+ reviews"')
    sp.add_argument("--plan", default="", help="use a saved/edited plan")
    sp.add_argument("--out", default="out/leads.csv")
    sp.add_argument("--yes", "-y", action="store_true", help="skip the confirmation")
    sp.add_argument("--workers", type=int, default=8, help="scrape workers")
    sp.add_argument("--site-workers", type=int, default=12)
    sp.add_argument("--llm-workers", type=int, default=1)
    sp.add_argument("--limit-results", type=int, default=20)
    sp.add_argument("--query-template", default="{category} in {zip}")
    sp.add_argument("--min-confidence", type=float, default=0.0)
    sp.add_argument("--owners", action="store_true", help="force the owner stage")
    sp.add_argument("--fallback", action="store_true", help="also web-search for owners")
    sp.add_argument("--fallback-source", default="",
                    help="apify (default) | openwebninja | none")
    sp.add_argument("--ignore-robots", action="store_true")
    add_zip_args(sp)
    add_llm_args(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("zips", help="build the US ZIP list (offline)")
    sp.add_argument("--out", default=str(DEFAULT_ZIPS))
    sp.add_argument("--types", default="STANDARD",
                    help="STANDARD,PO BOX,UNIQUE,MILITARY or 'all'")
    sp.add_argument("--territories", action="store_true", help="include PR/VI/GU/...")
    sp.add_argument("--include-inactive", action="store_true")
    sp.set_defaults(func=cmd_zips)

    sp = sub.add_parser("categories", help="list verticals in categories.yml")
    sp.add_argument("--categories-file", default=str(DEFAULT_CATEGORIES))
    sp.set_defaults(func=cmd_categories)

    sp = sub.add_parser("estimate", help="requests and cost for a run")
    add_cat_args(sp)
    add_zip_args(sp)
    sp.add_argument("--limit-results", type=int, default=20)
    sp.set_defaults(func=cmd_estimate)

    sp = sub.add_parser("probe", help="one live request, printed raw")
    add_zip_args(sp)
    sp.add_argument("--category", default="funeral home")
    sp.add_argument("--zip", default="")
    sp.add_argument("--limit-results", type=int, default=20)
    sp.add_argument("--query-template", default="{category} in {zip}")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("scrape", help="run the (zip x category) grid")
    add_cat_args(sp)
    add_zip_args(sp)
    sp.add_argument("--workers", type=int, default=8)
    sp.add_argument("--limit-results", type=int, default=20)
    sp.add_argument("--query-template", default="{category} in {zip}")
    sp.add_argument("--param", action="append", default=[],
                    help="extra query param, key=value (repeatable)")
    sp.add_argument("--max-jobs", type=int, help="stop after N searches this run")
    sp.set_defaults(func=cmd_scrape)

    sp = sub.add_parser("enrich", help="fetch website text with html2text")
    sp.add_argument("--workers", type=int, default=12)
    sp.add_argument("--limit", type=int)
    sp.add_argument("--timeout", type=int, default=15)
    sp.add_argument("--delay", type=float, default=0.0,
                    help="seconds between sub-page fetches on one domain")
    sp.add_argument("--ignore-robots", action="store_true")
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser("classify", help="local LLM confirms the ICP fit")
    add_cat_args(sp, need_icp=True)
    add_llm_args(sp)
    sp.add_argument("--workers", type=int, default=1,
                    help="1 is right on CPU; raise only with a GPU")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--evidence-chars", type=int, default=0,
                    help="0 = use LLM_MAX_EVIDENCE_CHARS from .env")
    sp.add_argument("--min-confidence", type=float, default=0.0)
    sp.add_argument("--include-no-site", action="store_true",
                    help="also judge businesses with no website text")
    sp.set_defaults(func=cmd_classify)

    sp = sub.add_parser("owners", help="local LLM finds the owner's name")
    add_llm_args(sp)
    sp.add_argument("--workers", type=int, default=1,
                    help="1 is right on CPU; raise only with a GPU")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--evidence-chars", type=int, default=0,
                    help="0 = use LLM_MAX_EVIDENCE_CHARS from .env")
    sp.add_argument("--fallback", action="store_true", help="also web-search for owners")
    sp.add_argument("--fallback-source", default="",
                    help="apify (default) | openwebninja | none")
    sp.add_argument("--all", action="store_true", help="not just in-ICP rows")
    sp.set_defaults(func=cmd_owners)

    sp = sub.add_parser("export", help="write the CSV")
    sp.add_argument("--out", default="out/leads.csv")
    sp.add_argument("--all", action="store_true", help="include non-ICP rows")
    sp.add_argument("--with-owner", action="store_true")
    sp.add_argument("--with-phone", action="store_true")
    sp.add_argument("--with-website", action="store_true")
    sp.add_argument("--with-email", action="store_true")
    sp.add_argument("--min-confidence", type=float, default=0.0)
    sp.add_argument("--min-rating", type=float, default=0.0)
    sp.add_argument("--min-reviews", type=int, default=0)
    sp.add_argument("--states", nargs="*")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser(
        "bench", help="measure LLM speed on this machine and pick a model"
    )
    sp.add_argument("--models", default="",
                    help=f"comma-separated; default: {','.join(bench.SUGGESTED)}")
    sp.add_argument("--evidence-chars", type=int, default=2500)
    sp.add_argument("--rows", type=int, default=1500,
                    help="project total runtime for this many businesses")
    sp.add_argument("--runs", type=int, default=3)
    sp.add_argument("--ollama-host", default="")
    sp.set_defaults(func=cmd_bench)

    sp = sub.add_parser("stats", help="what is in the database")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("renormalize", help="re-map stored raw JSON, no API calls")
    sp.set_defaults(func=cmd_renormalize)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # `... | head` closed the pipe. Detach stdout so the interpreter does
        # not print a second BrokenPipeError while flushing at exit.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
