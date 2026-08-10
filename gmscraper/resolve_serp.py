"""Resolve mailing addresses via Apify Google SERP + OpenAI extract.

Sibling to resolve_places: same source_binding / pending / patch_row pattern,
but uses ``apify/google-search-scraper`` (batch of address queries) instead of
Maps text search. Proven on suite addresses where Maps returns an empty building.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import requests

from . import source_binding as sb
from .config import settings
from .llm import make_llm
from .mapsdata import domain_of

# User-confirmed PPE rates for apify/google-search-scraper on this account.
COST_START_USD = 0.001
COST_SERP_USD = 0.0045
DEFAULT_ACTOR = "apify/google-search-scraper"
BATCH_SIZE = 100
POLL_INTERVAL_SEC = 5.0
POLL_MAX_WAIT_SEC = 60 * 45

EXTRACT_SYSTEM = """You extract the business occupying a mailing / suite address
from Google organic search results.
Return ONLY valid JSON matching the schema. No prose, no markdown fences.

Rules:
1. Prefer a real operating company at that suite/address over the building name,
   property record, or generic directory hit.
2. company_name is the business name (e.g. "Weitzman"). Never invent one.
3. website/domain must appear in the results (title, snippet, or URL). Prefer the
   company's own site over aggregators (Yelp, LoopNet, LinkedIn, Facebook).
4. phone only when shown verbatim in results.
5. officer_name only when a clearly named person (principal, partner, officer)
   is tied to the company in the results; else empty string.
6. confidence 0..1 for how clearly results identify that company at the address.
7. If results are only the building, a map pin, or unrelated, return empty
   company_name/website/domain/phone and low confidence.
"""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "website": {"type": "string"},
        "domain": {"type": "string"},
        "phone": {"type": "string"},
        "officer_name": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": [
        "company_name",
        "website",
        "domain",
        "phone",
        "officer_name",
        "confidence",
    ],
}

AGGREGATOR_HOSTS = {
    "yelp.com",
    "facebook.com",
    "linkedin.com",
    "loopnet.com",
    "crexi.com",
    "realtor.com",
    "zillow.com",
    "redfin.com",
    "mapquest.com",
    "yellowpages.com",
    "bbb.org",
}


def estimate_cost_usd(n_queries: int) -> float:
    n = max(0, int(n_queries))
    if n <= 0:
        return 0.0
    # One actor start per batch of up to BATCH_SIZE queries.
    batches = (n + BATCH_SIZE - 1) // BATCH_SIZE
    return batches * COST_START_USD + n * COST_SERP_USD


def apify_run_charge_cap(max_cost: float, batch_est: float) -> float:
    """Per-run Apify maxTotalChargeUsd.

    Global APIFY_MAX_COST_USD <= 0 means no ceiling; still pass Apify a
    per-batch bound so a runaway actor cannot charge an absurd amount.
    """
    if max_cost and max_cost > 0:
        return float(max_cost)
    return max(float(batch_est) * 3.0, 1.0)


def _actor_id(actor: str) -> str:
    return (actor or DEFAULT_ACTOR).strip().replace("/", "~")


def _serp_actor() -> str:
    return (
        (getattr(settings, "apify_google_search_actor", "") or "").strip()
        or DEFAULT_ACTOR
    )


def _env(name: str, default: str = "") -> str:
    import os

    return (os.environ.get(name) or default).strip()


def _normalize_website(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or host in AGGREGATOR_HOSTS:
        return ""
    # Reject pure aggregator subdomains.
    for agg in AGGREGATOR_HOSTS:
        if host == agg or host.endswith("." + agg):
            return ""
    path = parts.path or ""
    return f"https://{host}{path}".rstrip("/")


def _organic_from_item(item: dict[str, Any]) -> list[dict[str, str]]:
    organic = item.get("organicResults") or item.get("results") or []
    if not isinstance(organic, list):
        return []
    out: list[dict[str, str]] = []
    for r in organic[:10]:
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "title": str(r.get("title") or ""),
                "snippet": str(
                    r.get("description") or r.get("snippet") or r.get("text") or ""
                ),
                "url": str(r.get("url") or r.get("link") or ""),
            }
        )
    return out


def serialize_organic(organic: list[dict[str, str]]) -> str:
    parts = []
    for r in organic:
        parts.append(
            f"{r.get('title') or ''}\n{r.get('snippet') or ''}\n{r.get('url') or ''}"
        )
    return "\n\n".join(parts)


def _match_item_to_query(
    items: list[dict[str, Any]], query: str
) -> dict[str, Any] | None:
    q = (query or "").strip().lower()
    for item in items:
        for key in ("searchQuery", "query", "term", "keyword"):
            val = item.get(key)
            if isinstance(val, dict):
                term = str(val.get("term") or val.get("query") or "").strip().lower()
            else:
                term = str(val or "").strip().lower()
            if term and (term == q or q in term or term in q):
                return item
    return None


def extract_business(
    llm: Any,
    *,
    address: str,
    organic: list[dict[str, str]],
) -> dict[str, Any]:
    text = serialize_organic(organic)
    if not text.strip():
        return {
            "company_name": "",
            "website": "",
            "domain": "",
            "phone": "",
            "officer_name": "",
            "confidence": 0.0,
        }
    prompt = (
        f"Mailing / suite address:\n{address}\n\n"
        f"Top organic Google results:\n{text[:12000]}"
    )
    try:
        data = llm.json_chat(EXTRACT_SYSTEM, prompt, EXTRACT_SCHEMA)
    except Exception:  # noqa: BLE001
        return {
            "company_name": "",
            "website": "",
            "domain": "",
            "phone": "",
            "officer_name": "",
            "confidence": 0.0,
        }
    if not isinstance(data, dict):
        data = {}
    website = _normalize_website(str(data.get("website") or ""))
    domain = (str(data.get("domain") or "").strip().lower()).removeprefix("www.")
    if not domain and website:
        domain = domain_of(website)
    if domain and any(domain == a or domain.endswith("." + a) for a in AGGREGATOR_HOSTS):
        domain = ""
        website = ""
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    phone = re.sub(r"[^\d+() \-]", "", str(data.get("phone") or "")).strip()
    return {
        "company_name": str(data.get("company_name") or "").strip(),
        "website": website,
        "domain": domain,
        "phone": phone,
        "officer_name": str(data.get("officer_name") or "").strip(),
        "confidence": conf,
    }


def _start_actor_run(
    *,
    queries: list[str],
    max_cost: float,
) -> dict[str, Any]:
    token = settings.apify_token
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set")
    from .apify_contacts import apify_token_valid

    if not apify_token_valid(token):
        raise RuntimeError("APIFY_TOKEN failed validation against /v2/users/me")

    actor = _actor_id(_serp_actor())
    # Newline-separated queries — actor batches them in one run.
    run_input = {
        "queries": "\n".join(queries),
        "maxPagesPerQuery": 1,
        "resultsPerPage": 10,
        "countryCode": "us",
        "languageCode": "en",
        "mobileResults": False,
        "includeUnfilteredResults": False,
    }
    start_url = f"{settings.apify_base_url}/v2/acts/{actor}/runs"
    params = {
        "token": token,
        "maxTotalChargeUsd": max_cost,
        "timeout": 600,
        "memory": 4096,
    }
    resp = requests.post(start_url, params=params, json=run_input, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Apify SERP start failed ({resp.status_code}): {resp.text[:400]}"
        )
    data = resp.json().get("data") or {}
    run_id = data.get("id")
    if not run_id:
        raise RuntimeError(f"Apify SERP start returned no run id: {resp.text[:300]}")
    return data


def _poll_run(run_id: str) -> dict[str, Any]:
    token = settings.apify_token
    url = f"{settings.apify_base_url}/v2/actor-runs/{run_id}"
    deadline = time.time() + POLL_MAX_WAIT_SEC
    last: dict[str, Any] = {}
    while time.time() < deadline:
        r = requests.get(url, params={"token": token}, timeout=30)
        r.raise_for_status()
        last = r.json().get("data") or {}
        status = (last.get("status") or "").upper()
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            return last
        time.sleep(POLL_INTERVAL_SEC)
    return {**last, "status": "TIMED-OUT"}


def _fetch_dataset_items(dataset_id: str) -> list[dict[str, Any]]:
    token = settings.apify_token
    url = f"{settings.apify_base_url}/v2/datasets/{dataset_id}/items"
    r = requests.get(
        url,
        params={"token": token, "clean": "true", "format": "json"},
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def run_serp_batch(queries: list[str], *, max_cost: float) -> dict[str, Any]:
    """Run one actor batch; return {items, usage_usd, run_id, status}."""
    if not queries:
        return {"items": [], "usage_usd": 0.0, "run_id": None, "status": "EMPTY"}
    started = _start_actor_run(queries=queries, max_cost=max_cost)
    run_id = started["id"]
    terminal = _poll_run(run_id)
    status = (terminal.get("status") or "").upper()
    usage = float(terminal.get("usageTotalUsd") or 0.0)
    items: list[dict[str, Any]] = []
    if status == "SUCCEEDED":
        ds = terminal.get("defaultDatasetId") or ""
        if ds:
            items = _fetch_dataset_items(ds)
    return {
        "items": items,
        "usage_usd": usage,
        "run_id": run_id,
        "status": status,
    }


def serp_pending_where(binding: sb.SourceBinding) -> str:
    """Rows SERP should process.

    Eligibility is about *usable identity* (a website/domain), not the Maps
    ``resolved`` flag. Maps commonly stamps ``resolved=true`` and writes the
    building's street address as ``business_name`` with no website — those rows
    are exactly the ones SERP exists to fix. Requiring an empty business_name
    previously hid ~10k of the highest-portfolio operators.

    Queue = not yet tried via SERP AND still missing domain AND website.
    """
    not_serp_yet = (
        "(resolve_raw IS NULL OR COALESCE(resolve_raw->>'via', '') <> 'serp')"
    )
    no_web = (
        f"COALESCE({binding.domain_column}, '') = '' "
        f"AND COALESCE(website, '') = ''"
    )
    base = f"({not_serp_yet} AND {no_web})"
    if binding.where:
        return f"({base}) AND ({binding.where})"
    return base


def _count_where(binding: sb.SourceBinding, where: str | None) -> int:
    n = sb.rpc(
        binding,
        "pp_count_rows",
        {
            "p_schema": binding.schema,
            "p_table": binding.table,
            "p_where": where,
        },
    )
    return int(n or 0)


def inventory(binding: sb.SourceBinding) -> dict[str, Any]:
    """Truth table for Lane-3 last-mile — what Maps left vs what SERP can do."""
    name_col = binding.name_column or "operator_name"
    total = _count_where(binding, None)
    with_domain = _count_where(
        binding, f"COALESCE({binding.domain_column}, '') <> ''"
    )
    with_website = _count_where(binding, "COALESCE(website, '') <> ''")
    with_name = _count_where(binding, f"COALESCE({name_col}, '') <> ''")
    with_business = _count_where(binding, "COALESCE(business_name, '') <> ''")
    via_serp = _count_where(binding, "resolve_raw->>'via' = 'serp'")
    pending = count_serp_pending(binding)
    # Building-as-name residue from Maps (has business_name, no web, not SERP).
    building_only = _count_where(
        binding,
        (
            "COALESCE(business_name, '') <> '' "
            f"AND COALESCE({binding.domain_column}, '') = '' "
            "AND COALESCE(website, '') = '' "
            "AND (resolve_raw IS NULL OR COALESCE(resolve_raw->>'via', '') <> 'serp')"
        ),
    )
    useful = with_domain  # domain is what unlocks contact enrichment
    return {
        "total_rows": total,
        "with_domain": with_domain,
        "with_website": with_website,
        "with_operator_name": with_name,
        "with_business_name": with_business,
        "via_serp": via_serp,
        "building_name_no_web": building_only,
        "pending_for_serp": pending,
        "useful_with_domain": useful,
        "useful_rate": round(useful / total, 4) if total else 0.0,
        "note": (
            "Maps 'resolved=true' is NOT the same as useful. "
            "SERP queues rows missing domain+website that have not been "
            "tried via SERP — including rows where Maps wrote a building "
            "address as business_name."
        ),
    }


def count_serp_pending(binding: sb.SourceBinding) -> int:
    n = sb.rpc(
        binding,
        "pp_count_rows",
        {
            "p_schema": binding.schema,
            "p_table": binding.table,
            "p_where": serp_pending_where(binding),
        },
    )
    return int(n or 0)


def fetch_serp_pending(
    binding: sb.SourceBinding, *, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    cols = [
        binding.key_column,
        binding.resolved_column,
        binding.confidence_column,
        binding.domain_column,
    ]
    for c in (
        binding.address_column,
        binding.name_column,
        binding.city_column,
        "website",
        "phone",
        "place_id",
        "business_name",
        "resolve_raw",
    ):
        if c and c not in cols:
            cols.append(c)
    rows = sb.rpc(
        binding,
        "pp_select_rows",
        {
            "p_schema": binding.schema,
            "p_table": binding.table,
            "p_columns": cols,
            "p_where": serp_pending_where(binding),
            "p_order_by": binding.order_by or binding.key_column,
            "p_limit": int(limit) if limit and limit > 0 else 100,
            "p_offset": int(offset or 0),
        },
    )
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def estimate(
    binding: sb.SourceBinding,
    *,
    limit: int = 0,
) -> dict[str, Any]:
    inv = inventory(binding)
    n = int(inv.get("pending_for_serp") or 0)
    if limit and limit > 0:
        n = min(n, int(limit))
    cost = round(estimate_cost_usd(n), 4)
    max_cost = float(getattr(settings, "apify_max_cost_usd", 0.0) or 0.0)
    # max_cost <= 0 means unlimited (no hard ceiling).
    blocked = bool(max_cost > 0 and cost > max_cost)
    warning = None
    if inv.get("total_rows") and inv.get("useful_rate", 0) < 0.05:
        warning = (
            f"Only {inv.get('useful_with_domain')}/{inv.get('total_rows')} rows "
            f"currently have a domain ({inv.get('useful_rate'):.1%}). "
            f"{inv.get('building_name_no_web')} still have a Maps building-name "
            f"with no website — those are included in pending_for_serp."
        )
    if n == 0:
        warning = (
            "No SERP-eligible rows (every row already has domain/website or "
            "resolve_raw.via='serp'). If Maps marked resolved without websites, "
            "that is expected to still be eligible — check inventory."
        )
    return {
        "project_id": binding.project_id,
        "schema": binding.schema,
        "table": binding.table,
        "pending_rows": n,
        "batch_size": BATCH_SIZE,
        "batches": (n + BATCH_SIZE - 1) // BATCH_SIZE if n else 0,
        "estimated_cost_usd": cost,
        "max_cost_usd": max_cost if max_cost > 0 else None,
        "cost_ceiling": "none" if max_cost <= 0 else f"${max_cost:.2f}",
        "blocked": blocked,
        "block_reason": "exceeds_APIFY_MAX_COST_USD" if blocked else None,
        "actor": _serp_actor(),
        "pending_mode": "missing_web_not_yet_serp",
        "inventory": inv,
        "warning": warning,
        "cost_model": {
            "start_usd": COST_START_USD,
            "per_serp_usd": COST_SERP_USD,
            "note": "1 SERP page/query; OpenAI parse is separate (LLM bill).",
        },
    }


def run(
    *,
    schema: str,
    table: str,
    key_column: str,
    address_column: str = "",
    name_column: str = "",
    city_column: str = "",
    where: str = "",
    order_by: str = "",
    limit: int = 0,
    min_confidence: float = 0.35,
    project_id: str = "",
    estimate_only: bool = False,
    batch_size: int = BATCH_SIZE,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    binding = sb.resolve_binding(
        project_id=project_id,
        schema=schema,
        table=table,
        key_column=key_column,
        address_column=address_column,
        name_column=name_column,
        city_column=city_column,
        where=where,
        order_by=order_by,
    )
    sb.validate_binding(binding)
    if estimate_only:
        est = estimate(binding, limit=limit)
        return {**est, "started": False, "estimate_only": True, "ensured": {}}

    ensured = sb.ensure_writeback_columns(binding)
    # Also ensure business_name / operator_name-friendly cols exist.
    est = estimate(binding, limit=limit)
    if est.get("blocked"):
        return {**est, "started": False, "blocked": True, "ensured": ensured}

    if not settings.apify_token:
        raise RuntimeError("APIFY_TOKEN is not set")
    if not (settings.openai_api_key or "").strip():
        raise RuntimeError("OPENAI_API_KEY is required to parse SERP results")
    llm = make_llm(settings)

    batch_n = max(1, min(200, int(batch_size or BATCH_SIZE)))
    pending_total = int(est.get("pending_rows") or 0)
    counts = {
        "started": True,
        "rows": 0,
        "resolved": 0,
        "hit": 0,
        "useful_with_domain": 0,
        "no_match": 0,
        "errors": 0,
        "batches": 0,
        "apify_runs": 0,
        "usage_usd": 0.0,
        "estimated_cost_usd": est.get("estimated_cost_usd"),
        "actor": _serp_actor(),
        "batch_size": batch_n,
        "inventory_before": est.get("inventory"),
    }
    done = 0
    total = pending_total or 0
    max_rows = int(limit) if limit and limit > 0 else 0
    max_cost = float(getattr(settings, "apify_max_cost_usd", 0.0) or 0.0)

    def _tick(**extra: Any) -> None:
        if not on_progress:
            return
        try:
            on_progress(
                stage="resolve_via_serp",
                done=done,
                total=total or done,
                resolved=counts["resolved"],
                hit=counts["hit"],
                no_match=counts["no_match"],
                errors=counts["errors"],
                usage_usd=round(counts["usage_usd"], 4),
                project_id=binding.project_id,
                table=binding.table,
                **extra,
            )
        except Exception:  # noqa: BLE001
            pass

    _tick(batch=0)

    while True:
        try:
            from mcp_server.jobs import is_cancel_requested

            if is_cancel_requested():
                counts["cancelled"] = True
                break
        except Exception:  # noqa: BLE001
            pass

        remaining_cap = 0
        if max_rows:
            remaining_cap = max_rows - counts["rows"]
            if remaining_cap <= 0:
                break
        fetch_lim = batch_n if not remaining_cap else min(batch_n, remaining_cap)
        rows = fetch_serp_pending(binding, limit=fetch_lim)
        if not rows:
            break
        if not total:
            total = len(rows) if max_rows else (pending_total or len(rows))

        # Build query list keyed to rows.
        work: list[tuple[dict[str, Any], str]] = []
        for row in rows:
            addr = ""
            if binding.address_column:
                addr = str(row.get(binding.address_column) or "").strip()
            if not addr and binding.name_column:
                addr = str(row.get(binding.name_column) or "").strip()
            if not addr:
                # Nothing to search — mark resolved so we don't loop.
                now = datetime.now(timezone.utc).isoformat()
                sb.patch_row(
                    binding,
                    row.get(binding.key_column),
                    {
                        binding.resolved_column: True,
                        "resolved_at": now,
                        "resolve_raw": {"via": "serp", "status": "no_address"},
                    },
                )
                counts["no_match"] += 1
                counts["resolved"] += 1
                done += 1
                continue
            work.append((row, addr))

        counts["rows"] += len(rows)
        counts["batches"] += 1
        _tick(batch=counts["batches"], batch_rows=len(rows))

        if not work:
            if len(rows) < fetch_lim:
                break
            continue

        # Optional cost gate (disabled when APIFY_MAX_COST_USD <= 0).
        batch_est = estimate_cost_usd(len(work))
        if max_cost > 0 and counts["usage_usd"] + batch_est > max_cost + 0.01:
            counts["blocked_mid_run"] = True
            counts["block_reason"] = (
                f"remaining batch est ${batch_est:.4f} would exceed "
                f"APIFY_MAX_COST_USD ${max_cost:.2f}"
            )
            break

        queries = [q for _, q in work]
        try:
            result = run_serp_batch(
                queries, max_cost=apify_run_charge_cap(max_cost, batch_est)
            )
        except Exception:  # noqa: BLE001
            counts["errors"] += len(work)
            done += len(work)
            _tick()
            continue

        counts["apify_runs"] += 1
        counts["usage_usd"] += float(result.get("usage_usd") or 0)
        items = result.get("items") or []
        now = datetime.now(timezone.utc).isoformat()

        # Index remaining items by order as fallback when query field missing.
        unused = list(items)

        for row, query in work:
            item = _match_item_to_query(unused, query)
            if item is None and unused:
                item = unused.pop(0)
            elif item is not None and item in unused:
                unused.remove(item)

            organic = _organic_from_item(item or {})
            try:
                extracted = extract_business(llm, address=query, organic=organic)
            except Exception:  # noqa: BLE001
                extracted = {
                    "company_name": "",
                    "website": "",
                    "domain": "",
                    "phone": "",
                    "officer_name": "",
                    "confidence": 0.0,
                }
                counts["errors"] += 1

            conf = float(extracted.get("confidence") or 0)
            company = extracted.get("company_name") or ""
            website = extracted.get("website") or ""
            domain = extracted.get("domain") or ""
            phone = extracted.get("phone") or ""
            officer = extracted.get("officer_name") or ""
            # A company name alone is a partial win; a domain unlocks enrichment.
            hit = bool(company) and conf >= float(min_confidence)
            useful = hit and bool(domain)

            raw_store = {
                "via": "serp",
                "query": query,
                "actor": _serp_actor(),
                "run_id": result.get("run_id"),
                "organic": organic[:5],
                "extracted": extracted,
                "status": (
                    "useful" if useful else ("hit_no_domain" if hit else "no_match")
                ),
            }
            patch: dict[str, Any] = {
                binding.resolved_column: True,
                binding.confidence_column: round(conf, 4),
                "resolved_at": now,
                "resolve_raw": raw_store,
            }
            if hit:
                patch["business_name"] = company
                # Prefer writing company into name_column when present.
                if binding.name_column:
                    patch[binding.name_column] = company
                if domain:
                    patch[binding.domain_column] = domain
                if website:
                    patch["website"] = website
                if phone:
                    patch["phone"] = phone
                if officer:
                    raw_store["officer_name"] = officer
                counts["hit"] += 1
                if useful:
                    counts["useful_with_domain"] += 1
            else:
                counts["no_match"] += 1
            counts["resolved"] += 1
            try:
                sb.patch_row(binding, row.get(binding.key_column), patch)
            except Exception:  # noqa: BLE001
                counts["errors"] += 1
            done += 1
            if done <= 20 or done % 10 == 0:
                _tick()

        if counts.get("cancelled") or len(rows) < fetch_lim:
            break

    counts["rows"] = done
    _tick(finished=True)

    useful = int(counts["useful_with_domain"])
    rows_n = int(counts["rows"])
    if rows_n == 0:
        outcome = "nothing_to_do"
        warning = (
            "No eligible rows processed. Check inventory.pending_for_serp — "
            "eligibility is missing domain+website, not resolved=false."
        )
    elif useful == 0:
        outcome = "no_value"
        warning = (
            f"Processed {rows_n} rows but wrote 0 domains. "
            "Job completed mechanically; produced nothing enrichable."
        )
    elif useful / rows_n < 0.1:
        outcome = "low_value"
        warning = (
            f"Only {useful}/{rows_n} rows got a domain "
            f"({useful / rows_n:.1%}). Treat as a weak run."
        )
    else:
        outcome = "ok"
        warning = None

    return {
        **counts,
        "project_id": binding.project_id,
        "schema": binding.schema,
        "table": binding.table,
        "min_confidence": min_confidence,
        "ensured": ensured,
        "usage_usd": round(counts["usage_usd"], 4),
        "outcome": outcome,
        "warning": warning,
        "useful_rate": round(useful / rows_n, 4) if rows_n else 0.0,
    }
