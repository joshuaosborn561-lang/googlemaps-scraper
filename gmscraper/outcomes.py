"""Outcome-oriented orchestration — what callers should drive.

Low-level tools (resolve_places, resolve_via_serp, enrich_*, …) remain for
debugging. This module is the product surface: state an intent + scope +
budget, get useful yield back (or a loud no_value), never a silent empty win.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import operators as ops
from . import resolve_places, resolve_serp, source_binding as sb
from .config import settings
from .mapsdata import MapsDataClient, domain_of
from .resolve_places import is_address_like_name, pick_best


def parse_addresses(raw: str | list[str] | None) -> list[str]:
    """Accept newline / semicolon list or JSON array of address strings."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except json.JSONDecodeError:
            pass
    parts = re.split(r"[\n;]+", text)
    return [p.strip().strip('"').strip("'") for p in parts if p.strip()]


def _outcome(rows: int, useful: int) -> tuple[str, str | None]:
    if rows <= 0:
        return "nothing_to_do", "No rows to process."
    if useful <= 0:
        return (
            "no_value",
            f"Processed {rows} rows but produced 0 usable businesses with a domain. "
            "Do not treat this as success.",
        )
    if useful / rows < 0.1:
        return (
            "low_value",
            f"Only {useful}/{rows} rows got a domain ({useful / rows:.1%}). Weak run.",
        )
    return "ok", None


def status(
    *,
    scope: str = "operators",
    client_tag: str = "",
    project_id: str = "",
    schema: str = "",
    table: str = "",
) -> dict[str, Any]:
    """Honest inventory for a scope. Prefer this over guessing from job JSON."""
    scope_l = (scope or "operators").strip().lower()
    out: dict[str, Any] = {
        "scope": scope_l,
        "client_tag": (client_tag or "").strip() or None,
    }

    if scope_l in ("operators", "owner_lane", "lane3", "parcels"):
        pid = (
            project_id
            or __import__("os").environ.get("LEADS_SUPABASE_PROJECT_ID", "")
            or "kemvxzhcxvynmoutwdrh"
        )
        binding = sb.resolve_binding(
            project_id=pid,
            schema=schema or "permit_parcel",
            table=table or "operators",
            key_column="operator_address",
            address_column="operator_address",
            name_column="operator_name",
        )
        inv = resolve_serp.inventory(binding)
        serp_est = resolve_serp.estimate(binding, limit=0)
        out.update(
            {
                "project_id": binding.project_id,
                "schema": binding.schema,
                "table": binding.table,
                "inventory": inv,
                "serp_estimate": {
                    "pending_rows": serp_est.get("pending_rows"),
                    "estimated_cost_usd": serp_est.get("estimated_cost_usd"),
                    "max_cost_usd": serp_est.get("max_cost_usd"),
                    "blocked": serp_est.get("blocked"),
                    "block_reason": serp_est.get("block_reason"),
                    "warning": serp_est.get("warning"),
                },
                "done_means": (
                    "in-state operator mailing → real company name + domain/website "
                    "→ then a named person with email. resolved=true alone is NOT done."
                ),
            }
        )
        useful = int(inv.get("useful_with_domain") or 0)
        total = int(inv.get("total_rows") or 0)
        outcome, warning = _outcome(total, useful) if total else ("nothing_to_do", None)
        # For status, low useful_rate on a full table is expected mid-flight.
        if total and useful / total < 0.05:
            outcome = "in_progress_or_stuck"
            warning = inv.get("note") or warning
        out["outcome"] = outcome
        out["warning"] = warning or serp_est.get("warning")
        return out

    # Generic table inventory when schema/table provided.
    if schema and table:
        binding = sb.resolve_binding(
            project_id=project_id,
            schema=schema,
            table=table,
            key_column="id",
            address_column="",
            name_column="",
        )
        try:
            inv = resolve_serp.inventory(binding)
            out["inventory"] = inv
            out["project_id"] = binding.project_id
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)[:400]
        return out

    out["note"] = (
        "Pass scope='operators' for owner-lane inventory, or schema+table for a "
        "custom source. For local Maps lead lists use leads_summary / get_job_status."
    )
    return out


def _maps_lookup_one(
    client: MapsDataClient, address: str, *, min_confidence: float
) -> dict[str, Any]:
    zip_row = resolve_places._zip_row_from_address(address)
    results = resolve_places._search(client, address, zip_row)
    best, conf, n = pick_best(address, results, min_confidence=min_confidence)
    if not best:
        return {
            "address": address,
            "method": "maps",
            "status": "no_match" if not results else "low_confidence",
            "confidence": conf,
            "candidates": n or len(results),
            "business_name": "",
            "domain": "",
            "website": "",
            "phone": "",
        }
    details = client.place_details(str(best.get("place_id") or "")) if best.get("place_id") else {}
    website = (details or {}).get("website") or best.get("website") or ""
    phone = (details or {}).get("phone") or best.get("phone") or ""
    name = str(best.get("name") or "").strip()
    domain = domain_of(website)
    if is_address_like_name(name, address) and not website:
        return {
            "address": address,
            "method": "maps",
            "status": "building_only",
            "confidence": conf,
            "candidates": n,
            "business_name": "",
            "domain": "",
            "website": "",
            "phone": "",
            "place_id": best.get("place_id"),
        }
    return {
        "address": address,
        "method": "maps",
        "status": "useful" if domain else "hit_no_domain",
        "confidence": conf,
        "candidates": n,
        "business_name": name,
        "domain": domain,
        "website": website,
        "phone": phone,
        "place_id": best.get("place_id"),
    }


def _serp_lookup_batch(addresses: list[str], *, min_confidence: float) -> list[dict[str, Any]]:
    from .llm import make_llm

    if not addresses:
        return []
    if not settings.apify_token:
        raise RuntimeError("APIFY_TOKEN is not set (needed for SERP resolve)")
    if not (settings.openai_api_key or "").strip():
        raise RuntimeError("OPENAI_API_KEY is required to parse SERP results")

    max_cost = float(getattr(settings, "apify_max_cost_usd", 0.0) or 0.0)
    llm = make_llm(settings)
    out: list[dict[str, Any]] = []
    batch_size = resolve_serp.BATCH_SIZE
    for i in range(0, len(addresses), batch_size):
        chunk = addresses[i : i + batch_size]
        est = resolve_serp.estimate_cost_usd(len(chunk))
        if max_cost > 0 and est > max_cost + 0.01:
            raise RuntimeError(
                f"SERP batch of {len(chunk)} est ${est:.4f} exceeds "
                f"APIFY_MAX_COST_USD ${max_cost:.2f}. Lower limit or raise the ceiling."
            )
        result = resolve_serp.run_serp_batch(
            chunk, max_cost=resolve_serp.apify_run_charge_cap(max_cost, est)
        )
        items = result.get("items") or []
        unused = list(items)
        for addr in chunk:
            item = resolve_serp._match_item_to_query(unused, addr)
            if item is None and unused:
                item = unused.pop(0)
            elif item is not None and item in unused:
                unused.remove(item)
            organic = resolve_serp._organic_from_item(item or {})
            extracted = resolve_serp.extract_business(llm, address=addr, organic=organic)
            company = extracted.get("company_name") or ""
            domain = extracted.get("domain") or ""
            website = extracted.get("website") or ""
            phone = extracted.get("phone") or ""
            conf = float(extracted.get("confidence") or 0)
            hit = bool(company) and conf >= min_confidence
            useful = hit and bool(domain)
            out.append(
                {
                    "address": addr,
                    "method": "serp",
                    "status": (
                        "useful" if useful else ("hit_no_domain" if hit else "no_match")
                    ),
                    "confidence": conf,
                    "business_name": company if hit else "",
                    "domain": domain if hit else "",
                    "website": website if hit else "",
                    "phone": phone if hit else "",
                    "officer_name": extracted.get("officer_name") or "",
                }
            )
    return out


def resolve_addresses(
    *,
    addresses: str = "",
    schema: str = "",
    table: str = "",
    key_column: str = "",
    address_column: str = "",
    name_column: str = "",
    city_column: str = "",
    where: str = "",
    order_by: str = "",
    project_id: str = "",
    method: str = "auto",
    limit: int = 0,
    min_confidence: float = 0.35,
    estimate_only: bool = False,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Find the business at each address.

    Pass ``addresses`` (newline/JSON list) for ad-hoc lookups, OR bind a Supabase
    table. ``method``: auto (Maps then SERP for misses), maps, serp.
    """
    method_l = (method or "auto").strip().lower()
    if method_l not in ("auto", "maps", "serp"):
        raise ValueError("method must be auto|maps|serp")

    addr_list = parse_addresses(addresses)
    use_table = bool(table and key_column and not addr_list)

    if not addr_list and not use_table:
        return {
            "started": False,
            "outcome": "nothing_to_do",
            "warning": "Pass addresses= (list) or schema/table/key_column/address_column.",
            "results": [],
        }

    # ---- Table-bound path ----
    if use_table:
        return _resolve_table(
            schema=schema,
            table=table,
            key_column=key_column,
            address_column=address_column,
            name_column=name_column,
            city_column=city_column,
            where=where,
            order_by=order_by,
            project_id=project_id,
            method=method_l,
            limit=limit,
            min_confidence=min_confidence,
            estimate_only=estimate_only,
            on_progress=on_progress,
        )

    # ---- Ad-hoc address list ----
    if limit and limit > 0:
        addr_list = addr_list[: int(limit)]

    maps_cost_note = "Maps RapidAPI overage per plan; SERP $0.0045/query + $0.001/batch"
    serp_pending = addr_list if method_l == "serp" else []
    maps_est_n = len(addr_list) if method_l in ("auto", "maps") else 0
    # Rough SERP estimate for auto assumes ~70% need SERP (suite/building miss rate).
    serp_est_n = (
        len(addr_list)
        if method_l == "serp"
        else (int(len(addr_list) * 0.7) if method_l == "auto" else 0)
    )
    serp_cost = resolve_serp.estimate_cost_usd(serp_est_n if method_l != "maps" else 0)
    max_cost = float(getattr(settings, "apify_max_cost_usd", 0.0) or 0.0)
    blocked = bool(max_cost > 0 and method_l != "maps" and serp_cost > max_cost)

    if estimate_only:
        return {
            "started": False,
            "estimate_only": True,
            "mode": "ad_hoc",
            "method": method_l,
            "address_count": len(addr_list),
            "estimated_serp_cost_usd": round(serp_cost, 4),
            "max_cost_usd": max_cost if max_cost > 0 else None,
            "cost_ceiling": "none" if max_cost <= 0 else f"${max_cost:.2f}",
            "blocked": blocked,
            "block_reason": ("exceeds_APIFY_MAX_COST_USD" if blocked else None),
            "cost_note": maps_cost_note,
            "sample_addresses": addr_list[:5],
        }

    results: list[dict[str, Any]] = []
    maps_requests = 0

    if method_l in ("auto", "maps"):
        settings.require_rapidapi()
        client = MapsDataClient(settings, limit=8)
        need_serp: list[str] = []
        for addr in addr_list:
            row = _maps_lookup_one(client, addr, min_confidence=max(0.35, min_confidence))
            maps_requests += client.request_count
            client.request_count = 0
            if method_l == "auto" and row.get("status") in (
                "building_only",
                "no_match",
                "low_confidence",
                "hit_no_domain",
            ):
                need_serp.append(addr)
            else:
                results.append(row)
            if on_progress:
                try:
                    on_progress(
                        stage="resolve_addresses",
                        done=len(results) + len(need_serp),
                        total=len(addr_list),
                        method="maps",
                    )
                except Exception:  # noqa: BLE001
                    pass
        serp_pending = need_serp if method_l == "auto" else []

    if method_l == "serp":
        serp_pending = list(addr_list)

    if serp_pending:
        serp_rows = _serp_lookup_batch(serp_pending, min_confidence=min_confidence)
        # Prefer SERP when it produced a domain; else keep Maps row if any.
        by_addr = {r["address"]: r for r in results}
        for s in serp_rows:
            prev = by_addr.get(s["address"])
            if s.get("domain") or not prev:
                by_addr[s["address"]] = s
            elif prev and not prev.get("domain") and s.get("business_name"):
                by_addr[s["address"]] = s
        results = list(by_addr.values())
        # Preserve input order
        order = {a: i for i, a in enumerate(addr_list)}
        results.sort(key=lambda r: order.get(r["address"], 10_000))

    useful = sum(1 for r in results if r.get("domain"))
    outcome, warning = _outcome(len(results), useful)
    return {
        "started": True,
        "mode": "ad_hoc",
        "method": method_l,
        "rows": len(results),
        "useful_with_domain": useful,
        "useful_rate": round(useful / len(results), 4) if results else 0.0,
        "maps_requests": maps_requests,
        "outcome": outcome,
        "warning": warning,
        "results": results[:500],  # hard cap in response
        "results_truncated": max(0, len(results) - 500),
        "done_means": "business_name + domain/website per address",
    }


def _resolve_table(
    *,
    schema: str,
    table: str,
    key_column: str,
    address_column: str,
    name_column: str,
    city_column: str,
    where: str,
    order_by: str,
    project_id: str,
    method: str,
    limit: int,
    min_confidence: float,
    estimate_only: bool,
    on_progress: Any | None,
) -> dict[str, Any]:
    # Defaults for operators convenience
    if table == "operators" and not order_by:
        order_by = "portfolio_value DESC NULLS LAST"
    if table == "operators" and not address_column:
        address_column = "operator_address"
    if table == "operators" and not name_column:
        name_column = "operator_name"
    if table in ("operators", "parcels") and not schema:
        schema = "permit_parcel"

    out: dict[str, Any] = {
        "mode": "table",
        "method": method,
        "schema": schema,
        "table": table,
        "project_id": project_id,
        "per_stage": {},
    }

    if method in ("auto", "maps"):
        maps_res = resolve_places.run(
            schema=schema,
            table=table,
            key_column=key_column,
            address_column=address_column,
            name_column=name_column,
            city_column=city_column,
            where=where,
            order_by=order_by,
            limit=limit,
            min_confidence=max(0.5, float(min_confidence)),
            estimate_only=estimate_only,
            project_id=project_id,
            on_progress=on_progress,
        )
        out["per_stage"]["maps"] = {
            k: maps_res.get(k)
            for k in (
                "started",
                "rows",
                "resolved",
                "useful_with_domain",
                "building_only",
                "no_match",
                "outcome",
                "warning",
                "estimated_overage_usd",
                "blocked",
            )
        }
        if estimate_only and method == "maps":
            return {**out, "estimate_only": True, "started": False}
        if maps_res.get("blocked"):
            return {**out, "started": False, "blocked": True, "outcome": "blocked"}

    if method in ("auto", "serp") and not estimate_only:
        serp_res = resolve_serp.run(
            schema=schema,
            table=table,
            key_column=key_column,
            address_column=address_column,
            name_column=name_column,
            city_column=city_column,
            where=where,
            order_by=order_by,
            limit=limit,
            min_confidence=float(min_confidence),
            estimate_only=False,
            project_id=project_id,
            on_progress=on_progress,
        )
        out["per_stage"]["serp"] = {
            k: serp_res.get(k)
            for k in (
                "started",
                "rows",
                "hit",
                "useful_with_domain",
                "no_match",
                "outcome",
                "warning",
                "usage_usd",
                "blocked",
                "estimated_cost_usd",
            )
        }
        if serp_res.get("blocked") and not serp_res.get("started"):
            # Still return maps stage if any
            out["started"] = bool(out["per_stage"].get("maps", {}).get("started"))
            out["blocked"] = True
            out["block_reason"] = serp_res.get("block_reason")
            out["outcome"] = "blocked"
            out["warning"] = (
                serp_res.get("warning")
                or "SERP blocked by APIFY_MAX_COST_USD — raise ceiling or pass limit="
            )
            return out
    elif method in ("auto", "serp") and estimate_only:
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
        serp_est = resolve_serp.estimate(binding, limit=limit)
        out["per_stage"]["serp"] = serp_est
        out["estimate_only"] = True
        out["started"] = False
        out["inventory"] = serp_est.get("inventory")
        out["warning"] = serp_est.get("warning")
        out["blocked"] = serp_est.get("blocked")
        return out

    # Summarize useful yield from stages
    useful = 0
    rows = 0
    for stage in out["per_stage"].values():
        if not isinstance(stage, dict):
            continue
        useful += int(stage.get("useful_with_domain") or 0)
        rows += int(stage.get("rows") or 0)
    # Prefer post-run inventory when possible
    try:
        binding = sb.resolve_binding(
            project_id=project_id,
            schema=schema,
            table=table,
            key_column=key_column,
            address_column=address_column,
            name_column=name_column,
        )
        inv = resolve_serp.inventory(binding)
        out["inventory_after"] = inv
        useful = int(inv.get("useful_with_domain") or useful)
        rows = int(inv.get("total_rows") or rows)
    except Exception:  # noqa: BLE001
        pass

    outcome, warning = _outcome(rows, useful)
    # If serp wrote useful this run, prefer stage outcome
    serp_stage = out["per_stage"].get("serp") or {}
    if isinstance(serp_stage, dict) and serp_stage.get("useful_with_domain"):
        outcome, warning = _outcome(
            int(serp_stage.get("rows") or 0),
            int(serp_stage.get("useful_with_domain") or 0),
        )
    out.update(
        {
            "started": True,
            "rows": rows,
            "useful_with_domain": useful,
            "outcome": outcome,
            "warning": warning,
            "done_means": "business_name + domain/website on source rows",
        }
    )
    return out


def run_owner_lane(
    *,
    states: str = "TX",
    rebuild_operators: bool = False,
    operators_dry_run: bool = True,
    min_parcels: int = 1,
    resolve_limit: int = 0,
    method: str = "serp",
    min_confidence: float = 0.35,
    project_id: str = "",
    estimate_only: bool = False,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Owner / mailing-operator lane (parcel shells → company at mailing address).

    Generic — pass ``states`` for any market. Does not hardcode a vertical.
    Default: do not rebuild operators (destructive); resolve via SERP on the
    current operators table. Set rebuild_operators=true after reviewing a dry_run.
    """
    pid = (
        project_id
        or __import__("os").environ.get("LEADS_SUPABASE_PROJECT_ID", "")
        or "kemvxzhcxvynmoutwdrh"
    )
    out: dict[str, Any] = {
        "lane": "owner_operators",
        "states": states,
        "project_id": pid,
        "per_stage": {},
        "estimate_only": bool(estimate_only),
    }

    if rebuild_operators:
        op_res = ops.build_operators(
            states=states,
            project_id=pid,
            dry_run=bool(operators_dry_run) or bool(estimate_only),
            min_parcels=int(min_parcels or 1),
        )
        out["per_stage"]["build_operators"] = op_res
        if op_res.get("dry_run") and rebuild_operators and not estimate_only:
            out["started"] = False
            out["outcome"] = "needs_confirm"
            out["warning"] = (
                "build_operators ran dry_run=true (default). Re-call with "
                "operators_dry_run=false to truncate+replace, or set "
                "rebuild_operators=false to resolve the current table."
            )
            return out

    # Status + resolve
    st = status(scope="operators", project_id=pid)
    out["per_stage"]["status_before"] = {
        k: st.get(k) for k in ("inventory", "serp_estimate", "outcome", "warning")
    }

    resolve_res = resolve_addresses(
        schema="permit_parcel",
        table="operators",
        key_column="operator_address",
        address_column="operator_address",
        name_column="operator_name",
        order_by="portfolio_value DESC NULLS LAST",
        project_id=pid,
        method=method,
        limit=int(resolve_limit or 0),
        min_confidence=min_confidence,
        estimate_only=estimate_only,
        on_progress=on_progress,
    )
    out["per_stage"]["resolve"] = resolve_res
    out["started"] = bool(resolve_res.get("started"))
    out["blocked"] = bool(resolve_res.get("blocked"))
    out["outcome"] = resolve_res.get("outcome") or "ok"
    out["warning"] = resolve_res.get("warning")
    out["useful_with_domain"] = resolve_res.get("useful_with_domain")
    out["inventory_after"] = resolve_res.get("inventory_after")
    out["done_means"] = (
        "Operator rows with real company name + domain. Next: enrich people "
        "on those domains. resolved=true without domain is not done."
    )
    return out
