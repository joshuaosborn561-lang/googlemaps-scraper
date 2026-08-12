"""Email / DM enrichment waterfall.

Order for need='dm' (fixed): site/team-page (local + Apify crawl) → AI Ark →
getleads → LeadMagic → FullEnrich.

- Apify is discovery for domains with no known person (before paid lookups).
- Local team/about contacts are preferred when they match target titles.
- AI Ark / getleads / LeadMagic only accept people that match target_titles
  when that list is provided (otherwise legacy loose DM_TITLE_HINTS).
- FullEnrich is email-only and runs only when max_tier allows it (default does not).
- Results write to Supabase gc.companies / gc.contacts (or per-client tables).
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Sequence

from . import apify_contacts, gc_sync
from .config import settings
from .store import Store
from .vendors.ai_ark import AiArkClient
from .vendors.base import EmailHit, PersonHit, split_name
from .vendors.fullenrich import FullEnrichClient
from .vendors.getleads import GetLeadsClient
from .vendors.leadmagic import LeadMagicClient

Need = Literal["email", "dm", "both"]
MaxTier = Literal["apify", "aiark", "getleads", "leadmagic", "fullenrich"]

# Discovery first (Apify site crawl), then paid people vendors. FullEnrich last.
TIER_ORDER: list[str] = [
    "apify",
    "aiark",
    "getleads",
    "leadmagic",
    "fullenrich",
]
TIER_RANK = {name: i for i, name in enumerate(TIER_ORDER)}
DEFAULT_MAX_TIER: MaxTier = "leadmagic"

# Loose fallback when no target_titles are supplied.
DM_TITLE_HINTS = (
    "owner", "founder", "principal", "president", "ceo", "partner",
    "director", "vp", "vice president", "managing", "manager", "dealer",
)

# Basco / warranty-admin default when callers pass nothing explicit.
DEFAULT_DM_TARGET_TITLES = (
    "service director",
    "fixed operations director",
    "fixed ops director",
    "fixed operations",
    "fixed ops",
    "service manager",
    "warranty manager",
    "warranty administrator",
    "director of service",
    "vp of service",
    "vice president of service",
    "general manager",
    "dealer principal",
    "gm",
)


def normalize_max_tier(max_tier: str | None) -> str:
    t = (max_tier or DEFAULT_MAX_TIER).strip().lower()
    aliases = {
        "ai_ark": "aiark",
        "ai-ark": "aiark",
        "full_enrich": "fullenrich",
        "full-enrich": "fullenrich",
        "get_leads": "getleads",
        "lead_magic": "leadmagic",
    }
    t = aliases.get(t, t)
    if t not in TIER_RANK:
        raise ValueError(
            f"max_tier must be one of {', '.join(TIER_ORDER)}; got {max_tier!r}"
        )
    return t


def tier_allowed(tier: str, max_tier: str) -> bool:
    return TIER_RANK[tier] <= TIER_RANK[normalize_max_tier(max_tier)]


def _is_dm_title(title: str, target_titles: Sequence[str] | None = None) -> bool:
    """True when title matches target list, or loose hints if no targets."""
    return _title_rank(title, target_titles) > 0


def _title_rank(title: str, target_titles: Sequence[str] | None = None) -> int:
    """Higher is better. 0 = no match.

    When target_titles is provided, only those keys score (order = preference).
    When omitted, fall back to loose DM_TITLE_HINTS.
    Short keys (≤3 chars, e.g. ``gm``) require a word boundary so they do not
    match inside unrelated words like ``segment``.
    """
    t = (title or "").lower().strip()
    if not t:
        return 0
    keys = (
        [k.lower() for k in target_titles]
        if target_titles is not None
        else list(DM_TITLE_HINTS)
    )
    if not keys:
        return 0
    for i, key in enumerate(keys):
        key = (key or "").strip().lower()
        if not key:
            continue
        if len(key) <= 3:
            if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", t):
                return 1000 - i
        elif key in t:
            return 1000 - i
    return 0


def _pick_person(
    people: Sequence[PersonHit],
    *,
    target_titles: Sequence[str] | None = None,
    require_title_match: bool = False,
) -> PersonHit | None:
    """Choose the best person by target-title rank; optionally reject non-matches.

    When target_titles is None, ranking uses DM_TITLE_HINTS. With
    require_title_match=True, titles that score 0 are rejected either way.
    """
    ranked: list[tuple[int, PersonHit]] = []
    for p in people:
        if not apify_contacts._looks_like_person(p.first_name, p.last_name):
            continue
        rank = _title_rank(p.title, target_titles)
        if require_title_match and rank <= 0:
            continue
        ranked.append((rank, p))
    if not ranked:
        return None
    ranked.sort(key=lambda x: (-x[0], x[1].name.lower()))
    return ranked[0][1]


def _parse_rows(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if isinstance(rows, str):
        rows = json.loads(rows) if rows.strip() else []
    if not isinstance(rows, list):
        raise ValueError("rows must be a JSON list of objects")
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append(r)
    return out


def _norm_target_titles(target_titles: Any) -> list[str] | None:
    """None / blank = omitted (caller applies defaults). Non-empty = ranked targets."""
    if target_titles is None:
        return None
    if isinstance(target_titles, str):
        if not target_titles.strip():
            return None
        return [t.strip() for t in target_titles.split(",") if t.strip()]
    if isinstance(target_titles, (list, tuple)):
        parts = [str(t).strip() for t in target_titles if str(t).strip()]
        return parts or None
    raise ValueError("target_titles must be a comma-string or list of strings")


def _default_dm_titles(client_tag: str = "") -> list[str] | None:
    """Basco dealership DMs get service/fixed-ops titles; others use loose hints."""
    slug = (client_tag or "").strip().lower()
    if slug in ("basco", "carlos", "basco_warranty", "dealerships", "ny_dealers"):
        return list(DEFAULT_DM_TARGET_TITLES)
    return None


def _norm_row(r: dict[str, Any]) -> dict[str, Any]:
    domain = (
        r.get("domain")
        or r.get("website")
        or ""
    )
    if isinstance(domain, str) and "://" in domain:
        from urllib.parse import urlsplit
        host = urlsplit(domain if "://" in domain else f"https://{domain}").hostname or ""
        domain = host[4:] if host.startswith("www.") else host
    domain = str(domain or "").strip().lower()
    first = str(r.get("first_name") or "").strip()
    last = str(r.get("last_name") or "").strip()
    full = str(r.get("name") or r.get("full_name") or r.get("owner_name") or "").strip()
    if not first and full:
        first, last = split_name(full)
    return {
        "domain": domain,
        "company_name": str(r.get("company_name") or r.get("company") or r.get("business_name") or "").strip(),
        "first_name": first,
        "last_name": last,
        "full_name": full or f"{first} {last}".strip(),
        "title": str(r.get("title") or r.get("job_title") or r.get("owner_title") or "").strip(),
        "email": str(r.get("email") or "").strip().lower(),
        "place_id": str(r.get("place_id") or "").strip(),
        "city": str(r.get("city") or r.get("address_city") or "").strip(),
        "state": str(r.get("state") or r.get("address_state") or "").strip(),
        "in_maps_icp": bool(r.get("in_maps_icp") or r.get("in_icp") in (True, 1, "yes", "1")),
        "in_shovels": bool(r.get("in_shovels")),
        "permit_count": r.get("permit_count"),
        "source": str(r.get("source") or "maps"),
        "linkedin_url": str(r.get("linkedin_url") or "").strip(),
    }


def _person_from_local_contact(c: dict[str, Any]) -> PersonHit | None:
    """Accept only contacts that look like real humans (reject titles/companies)."""
    name = (c.get("name") or "").strip()
    if not name:
        return None
    first, last = split_name(name)
    if not apify_contacts._looks_like_person(first, last):
        return None
    return PersonHit(
        first_name=first,
        last_name=last,
        full_name=name,
        title=c.get("title") or "",
        email=c.get("email") or "",
        source_tier=c.get("source_tier") or c.get("source") or "local",
        raw=dict(c),
    )


class Waterfall:
    def __init__(
        self,
        *,
        getleads: GetLeadsClient | None = None,
        ai_ark: AiArkClient | None = None,
        leadmagic: LeadMagicClient | None = None,
        fullenrich: FullEnrichClient | None = None,
        store: Store | None = None,
        max_tier: str = DEFAULT_MAX_TIER,
        target_titles: Sequence[str] | None = None,
        require_title_match: bool = True,
    ):
        self.getleads = getleads or GetLeadsClient()
        self.ai_ark = ai_ark or AiArkClient()
        self.leadmagic = leadmagic or LeadMagicClient()
        self.fullenrich = fullenrich or FullEnrichClient()
        self.store = store
        self.max_tier = normalize_max_tier(max_tier)
        # None → loose hints; non-empty list → ranked targets (reject non-matches
        # when require_title_match).
        self.target_titles = (
            list(target_titles) if target_titles is not None else None
        )
        self.require_title_match = bool(require_title_match)
        self.tier_stats = {
            "apify": {"calls": 0, "email_hits": 0, "dm_hits": 0, "skips": []},
            "getleads": {"calls": 0, "email_hits": 0, "dm_hits": 0, "skips": []},
            "ai_ark": {"calls": 0, "email_hits": 0, "dm_hits": 0, "skips": []},
            "leadmagic": {"calls": 0, "email_hits": 0, "dm_hits": 0, "skips": []},
            "fullenrich": {"calls": 0, "email_hits": 0, "dm_hits": 0, "skips": []},
            "team_page": {"calls": 0, "email_hits": 0, "dm_hits": 0, "skips": []},
        }
        self.apify_meta: dict[str, Any] = {}

    def _titles(self) -> list[str] | None:
        return self.target_titles

    def _pick(self, people: Sequence[PersonHit]) -> PersonHit | None:
        # When target_titles is None, _title_rank falls back to DM_TITLE_HINTS.
        # require_title_match still rejects porter / clerk / etc.
        return _pick_person(
            people,
            target_titles=self._titles(),
            require_title_match=self.require_title_match,
        )

    def _bump(self, tier: str, field: str) -> None:
        self.tier_stats.setdefault(
            tier, {"calls": 0, "email_hits": 0, "dm_hits": 0, "skips": []}
        )
        self.tier_stats[tier][field] = self.tier_stats[tier].get(field, 0) + 1

    def _skip(self, tier: str, reason: str) -> None:
        """Record why a tier was not attempted or returned nothing useful."""
        bucket = self.tier_stats.setdefault(
            tier, {"calls": 0, "email_hits": 0, "dm_hits": 0, "skips": []}
        )
        skips = bucket.setdefault("skips", [])
        # Cap stored skip reasons so responses stay small.
        if len(skips) < 20 and reason not in skips:
            skips.append(reason)
        bucket["skip_count"] = int(bucket.get("skip_count") or 0) + 1
        # Also keep the latest reason for easy polling.
        bucket["last_skip_reason"] = reason

    def _allowed(self, tier: str) -> bool:
        return tier_allowed(tier, self.max_tier)

    def resolve_email(self, row: dict[str, Any]) -> EmailHit | None:
        """getleads → LeadMagic → FullEnrich (respecting max_tier)."""
        first, last, domain = row["first_name"], row["last_name"], row["domain"]
        if row.get("email"):
            return EmailHit(email=row["email"], source_tier="input", status="provided")
        if not (first and last and domain):
            return None

        if self.getleads.enabled and self._allowed("getleads"):
            self._bump("getleads", "calls")
            hit = self.getleads.find_email(first, last, domain, row.get("company_name") or "")
            if hit:
                self._bump("getleads", "email_hits")
                return hit

        if self.leadmagic.enabled and self._allowed("leadmagic"):
            self._bump("leadmagic", "calls")
            hit = self.leadmagic.find_email(first, last, domain, row.get("company_name") or "")
            if hit:
                self._bump("leadmagic", "email_hits")
                return hit

        if self.fullenrich.enabled and self._allowed("fullenrich"):
            self._bump("fullenrich", "calls")
            hit = self.fullenrich.find_email(first, last, domain, row.get("company_name") or "")
            if hit:
                self._bump("fullenrich", "email_hits")
                return hit
        return None

    def resolve_dm(self, row: dict[str, Any]) -> PersonHit | None:
        """Discover a decision-maker: team-page → AI Ark → getleads → LeadMagic.

        Apify site crawl runs as a batch before this (see enrich_waterfall).
        When target_titles is set, people whose titles do not match are rejected
        so a service porter / accounting clerk cannot count as a dm_hit.
        """
        domain = row["domain"]
        if not domain:
            return None
        titles = self._titles()
        require = self.require_title_match

        # 1) Local team/about contacts (free crawl / prior extract).
        if self.store:
            self._bump("team_page", "calls")
            local: list[PersonHit] = []
            for c in self.store.contacts_for_domain(domain):
                person = _person_from_local_contact(c)
                if person:
                    local.append(person)
            picked = self._pick(local)
            if picked:
                tier = (
                    "apify"
                    if picked.source_tier == "apify_openai"
                    else "team_page"
                )
                self._bump(tier, "dm_hits")
                return picked
            if local:
                self._skip("team_page", "local_people_title_mismatch")
            else:
                self._skip("team_page", "no_local_contacts")

        # Input row already has a usable person+title.
        if row.get("full_name") and _is_dm_title(row.get("title") or "", titles):
            first, last = row["first_name"], row["last_name"]
            if apify_contacts._looks_like_person(first, last):
                return PersonHit(
                    first_name=first,
                    last_name=last,
                    full_name=row["full_name"],
                    title=row.get("title") or "",
                    email=row.get("email") or "",
                    source_tier="input",
                )

        # 2) AI Ark
        if not self._allowed("aiark"):
            self._skip("ai_ark", "max_tier_excludes_aiark")
        elif not self.ai_ark.enabled:
            self._skip("ai_ark", "vendor_disabled_or_missing_key")
        else:
            self._bump("ai_ark", "calls")
            try:
                people = self.ai_ark.find_people(
                    domain, company_name=row.get("company_name") or ""
                )
            except Exception as exc:  # noqa: BLE001
                self._skip("ai_ark", f"error:{type(exc).__name__}:{exc}"[:180])
                people = []
            picked = self._pick(people)
            if picked:
                self._bump("ai_ark", "dm_hits")
                return picked
            if people:
                self._skip(
                    "ai_ark",
                    "title_mismatch" if require else "returned_non_person_names",
                )
            else:
                self._skip("ai_ark", "no_people_returned")

        # 3) getleads
        if not self._allowed("getleads"):
            self._skip("getleads", "max_tier_excludes_getleads")
        elif not self.getleads.enabled:
            self._skip("getleads", "vendor_disabled_or_missing_key")
        else:
            self._bump("getleads", "calls")
            people = self.getleads.find_people(domain, row.get("company_name") or "")
            picked = self._pick(people)
            if picked:
                self._bump("getleads", "dm_hits")
                return picked
            if people:
                self._skip("getleads", "title_mismatch_or_non_person")
            else:
                self._skip("getleads", "no_people_returned")

        # 4) LeadMagic
        if not self._allowed("leadmagic"):
            self._skip("leadmagic", "max_tier_excludes_leadmagic")
        elif not self.leadmagic.enabled:
            self._skip("leadmagic", "vendor_disabled_or_missing_key")
        else:
            self._bump("leadmagic", "calls")
            people = self.leadmagic.find_people(domain, row.get("company_name") or "")
            picked = self._pick(people)
            if picked:
                self._bump("leadmagic", "dm_hits")
                return picked
            if people:
                self._skip("leadmagic", "title_mismatch_or_non_person")
            else:
                self._skip("leadmagic", "no_people_returned")
        return None

    def discover_apify(self, domains: list[str]) -> dict[str, Any]:
        """Batch Apify crawl + OpenAI parse for domains with no known person."""
        if not self._allowed("apify"):
            return {"skipped": True, "reason": "max_tier_excludes_apify"}
        if not self.store:
            return {"skipped": True, "reason": "no_store"}
        if not settings.apify_token:
            return {"skipped": True, "reason": "apify_token_missing"}

        need: list[str] = []
        for d in domains:
            d = (d or "").strip().lower()
            if not d:
                continue
            has_person = False
            for c in self.store.contacts_for_domain(d):
                if _person_from_local_contact(c):
                    has_person = True
                    break
            if not has_person:
                need.append(d)
        if not need:
            return {"skipped": True, "reason": "all_have_person", "domains": 0}

        self._bump("apify", "calls")
        domains_csv = ",".join(need)
        try:
            crawl_res = apify_contacts.crawl(
                self.store,
                domains=domains_csv,
                estimate_only=False,
                verify_emails=False,
                run_label="waterfall_apify",
            )
        except Exception as exc:  # noqa: BLE001 — surface in meta, don't abort waterfall
            return {"skipped": False, "error": str(exc)[:300], "domains": len(need)}

        self.apify_meta["crawl"] = {
            k: crawl_res.get(k)
            for k in (
                "run_id", "started", "blocked", "estimated_cost_usd",
                "actual_cost_usd", "rows_persisted", "status", "reason",
            )
        }
        if crawl_res.get("blocked") or not crawl_res.get("started"):
            return {
                "skipped": False,
                "blocked": bool(crawl_res.get("blocked")),
                "crawl": self.apify_meta["crawl"],
                "domains": len(need),
            }

        run_id = str(crawl_res.get("run_id") or "")
        try:
            parse_res = apify_contacts.parse_contacts_openai(
                self.store, run_id=run_id, workers=8
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "skipped": False,
                "crawl": self.apify_meta["crawl"],
                "parse_error": str(exc)[:300],
                "domains": len(need),
            }

        self.apify_meta["parse"] = {
            k: parse_res.get(k)
            for k in (
                "domains_processed", "people_extracted", "domains_with_person",
                "rows_written", "estimated_llm_cost_usd",
            )
        }
        self.tier_stats["apify"]["dm_hits"] = int(
            parse_res.get("domains_with_person") or 0
        )
        return {
            "skipped": False,
            "domains": len(need),
            "crawl": self.apify_meta["crawl"],
            "parse": self.apify_meta["parse"],
        }


def enrich_waterfall(
    rows: Any,
    *,
    need: Need = "both",
    store: Store | None = None,
    write_supabase: bool = True,
    max_tier: str = DEFAULT_MAX_TIER,
    run_apify: bool = True,
    on_progress: Any | None = None,
    client_tag: str = "",
    target_titles: Any = None,
    require_title_match: bool = True,
) -> dict[str, Any]:
    """Walk tiers per row; upsert companies/contacts to Supabase; return counts only.

    max_tier (default leadmagic) hard-stops the walk so FullEnrich never fires
    unless explicitly requested. Pass client_tag so writes go to
    client_<slug>.contacts / .companies instead of the shared gc.* schema.

    target_titles: comma-string or list. For need='dm'/'both' with client_tag
    basco, defaults to service/fixed-ops/warranty → GM/Dealer Principal.
    Otherwise loose DM_TITLE_HINTS still reject non-DM staff when
    require_title_match is True. Pass require_title_match=False to accept any.
    """
    max_tier_n = normalize_max_tier(max_tier)
    parsed = [_norm_row(r) for r in _parse_rows(rows)]
    parsed = [r for r in parsed if r.get("domain")]
    if not parsed:
        return {
            "rows_in": 0,
            "companies_upserted": 0,
            "contacts_written": 0,
            "emails_found": 0,
            "dms_found": 0,
            "tier_stats": {},
            "need": need,
            "max_tier": max_tier_n,
        }

    titles = _norm_target_titles(target_titles)
    if titles is None and need in ("dm", "both"):
        titles = _default_dm_titles(client_tag)

    def _tick(**extra: Any) -> None:
        if on_progress is None:
            return
        try:
            on_progress(**extra)
        except Exception:  # noqa: BLE001
            pass

    _tick(
        done=0,
        total=len(parsed),
        jobs_total=len(parsed),
        jobs_done=0,
        jobs_pending=len(parsed),
        businesses_found=0,
        emails_found=0,
    )

    wf = Waterfall(
        store=store,
        max_tier=max_tier_n,
        target_titles=titles,
        require_title_match=require_title_match,
    )
    company_rows: list[dict[str, Any]] = []
    contact_rows: list[dict[str, Any]] = []
    emails_found = 0
    dms_found = 0

    # Discovery tier: Apify + OpenAI for domains that still lack a person.
    apify_result: dict[str, Any] = {}
    if run_apify and need in ("dm", "both") and wf._allowed("apify"):
        # Only discover when the row itself has no usable person name.
        need_domains = [
            r["domain"]
            for r in parsed
            if not (
                r.get("first_name")
                and r.get("last_name")
                and apify_contacts._looks_like_person(r["first_name"], r["last_name"])
            )
        ]
        need_domains = list(dict.fromkeys(need_domains))
        if need_domains:
            apify_result = wf.discover_apify(need_domains)

    # Bulk FullEnrich pass for rows that still need email after earlier tiers.
    pending_fe: list[tuple[int, dict[str, Any]]] = []

    enriched: list[dict[str, Any]] = []
    for idx, row in enumerate(parsed):
        email_tier = ""
        dm_tier = ""
        email = row.get("email") or ""
        person: PersonHit | None = None

        has_person_name = bool(
            row.get("first_name")
            and row.get("last_name")
            and apify_contacts._looks_like_person(row["first_name"], row["last_name"])
        )

        # People discovery (AI Ark / getleads / LeadMagic find_people) whenever
        # we need a DM, or need an email but have no usable name yet.
        need_people = need in ("dm", "both") or (
            need == "email" and not has_person_name and not email
        )
        if need_people:
            person = wf.resolve_dm(row)
            if person:
                dms_found += 1
                dm_tier = person.source_tier
                if not row["first_name"] and person.first_name:
                    row["first_name"] = person.first_name
                    row["last_name"] = person.last_name
                    row["full_name"] = person.name
                    has_person_name = True
                if not row.get("title") and person.title:
                    row["title"] = person.title
                if person.email and not email:
                    email = person.email
                    email_tier = person.source_tier
        elif need == "email" and has_person_name:
            # AI Ark is people-discovery only — explicit skip when names exist.
            wf._skip("ai_ark", "need=email_with_names; ai_ark_is_people_discovery_only")

        if need in ("email", "both") and not email:
            # Inline getleads + leadmagic; defer fullenrich to bulk when allowed.
            first, last, domain = row["first_name"], row["last_name"], row["domain"]
            if first and last and domain:
                if wf.getleads.enabled and wf._allowed("getleads"):
                    wf._bump("getleads", "calls")
                    hit = wf.getleads.find_email(
                        first, last, domain, row.get("company_name") or ""
                    )
                    if hit:
                        wf._bump("getleads", "email_hits")
                        email, email_tier = hit.email, hit.source_tier
                elif not wf._allowed("getleads"):
                    wf._skip("getleads", "max_tier_excludes_getleads")
                elif not wf.getleads.enabled:
                    wf._skip("getleads", "vendor_disabled_or_missing_key")

                if not email and wf.leadmagic.enabled and wf._allowed("leadmagic"):
                    wf._bump("leadmagic", "calls")
                    hit = wf.leadmagic.find_email(
                        first, last, domain, row.get("company_name") or ""
                    )
                    if hit:
                        wf._bump("leadmagic", "email_hits")
                        email, email_tier = hit.email, hit.source_tier
                elif not email and not wf._allowed("leadmagic"):
                    wf._skip("leadmagic", "max_tier_excludes_leadmagic")
                elif not email and not wf.leadmagic.enabled:
                    wf._skip("leadmagic", "vendor_disabled_or_missing_key")

                if (
                    not email
                    and wf.fullenrich.enabled
                    and wf._allowed("fullenrich")
                ):
                    pending_fe.append((idx, row))
                elif not email and not wf._allowed("fullenrich"):
                    wf._skip("fullenrich", "max_tier_excludes_fullenrich")
            else:
                wf._skip("getleads", "missing_first_last_for_email_lookup")
                wf._skip("leadmagic", "missing_first_last_for_email_lookup")

        enriched.append(
            {
                "row": row,
                "email": email,
                "email_tier": email_tier,
                "dm_tier": dm_tier,
                "person": person,
            }
        )
        done_n = idx + 1
        if done_n == 1 or done_n % 5 == 0 or done_n == len(parsed):
            _tick(
                done=done_n,
                total=len(parsed),
                jobs_total=len(parsed),
                jobs_done=done_n,
                jobs_pending=max(0, len(parsed) - done_n),
                businesses_found=done_n,
                emails_found=emails_found,
                dms_found=dms_found,
            )

    # FullEnrich bulk for remaining email misses (only when max_tier allows).
    if pending_fe and wf.fullenrich.enabled and wf._allowed("fullenrich"):
        fe_rows = [
            {
                "first_name": r["first_name"],
                "last_name": r["last_name"],
                "domain": r["domain"],
                "company_name": r.get("company_name") or "",
            }
            for _, r in pending_fe
        ]
        wf._bump("fullenrich", "calls")
        hits = wf.fullenrich.find_email_bulk(fe_rows)
        for (idx, _row), hit in zip(pending_fe, hits):
            if hit:
                wf._bump("fullenrich", "email_hits")
                enriched[idx]["email"] = hit.email
                enriched[idx]["email_tier"] = hit.source_tier
    elif pending_fe:
        # Explicit: FullEnrich was eligible by miss but blocked by max_tier.
        wf.tier_stats["fullenrich"]["blocked_by_max_tier"] = len(pending_fe)

    for item in enriched:
        row = item["row"]
        email = item["email"]
        email_tier = item["email_tier"]
        dm_tier = item["dm_tier"]
        person = item["person"]
        if email:
            emails_found += 1

        company_rows.append(
            gc_sync.company_row(
                domain=row["domain"],
                company_name=row.get("company_name") or "",
                source=row.get("source") or "maps",
                in_maps_icp=bool(row.get("in_maps_icp")),
                in_shovels=bool(row.get("in_shovels")),
                permit_count=row.get("permit_count")
                if isinstance(row.get("permit_count"), int)
                else None,
                place=row.get("place_id") or "",
                address_city=row.get("city") or "",
                address_state=row.get("state") or "",
                email_source_tier=email_tier,
                dm_source_tier=dm_tier,
                source_tier={
                    k: v
                    for k, v in {"email": email_tier, "dm": dm_tier}.items()
                    if v
                },
                dm_lookup_status="found" if dm_tier else "not_found",
                client_tag=client_tag,
            )
        )

        first = row["first_name"]
        last = row["last_name"]
        if person:
            first = person.first_name or first
            last = person.last_name or last
        if first or last or email:
            contact_rows.append(
                gc_sync.contact_row(
                    domain=row["domain"],
                    first_name=first,
                    last_name=last,
                    job_title=row.get("title") or (person.title if person else ""),
                    email=email,
                    email_status="found" if email else "",
                    linkedin_url=(person.linkedin_url if person else "")
                    or row.get("linkedin_url")
                    or "",
                    contact_city=row.get("city") or "",
                    contact_state=row.get("state") or "",
                    source_tool=email_tier or dm_tier or "waterfall",
                    source_tier=email_tier or dm_tier,
                    place_id=row.get("place_id") or "",
                    confidence=0.7 if email or dm_tier else 0.0,
                    client_tag=client_tag,
                )
            )

        # Mirror into local SQLite when available.
        if store and (first or last):
            store.save_contact(
                name=f"{first} {last}".strip(),
                domain=row["domain"],
                place_id=row.get("place_id") or "",
                title=row.get("title") or (person.title if person else ""),
                email=email,
                source=email_tier or dm_tier or "waterfall",
                source_tier=email_tier or dm_tier,
                confidence=0.7 if email else 0.5,
            )
            if email:
                store.save_emails(row["domain"], [email], source=email_tier or "waterfall")

    companies_upserted = 0
    contacts_written = 0
    write_target = gc_sync.resolve_write_schema(client_tag)
    if write_supabase:
        companies_upserted = gc_sync.upsert_companies(
            company_rows, client_tag=client_tag
        )
        # Emails: ignore (domain, email) conflicts; null emails insert separately.
        with_email = [r for r in contact_rows if r.get("email")]
        no_email = [r for r in contact_rows if not r.get("email")]
        contacts_written = gc_sync.insert_contacts_ignore_conflict(
            with_email, client_tag=client_tag
        )
        contacts_written += gc_sync.insert_contacts(
            no_email, client_tag=client_tag
        )

    # Merge live client counters into tier_stats for reporting.
    for name, client in (
        ("getleads", wf.getleads),
        ("ai_ark", wf.ai_ark),
        ("leadmagic", wf.leadmagic),
        ("fullenrich", wf.fullenrich),
    ):
        wf.tier_stats[name]["vendor_calls"] = getattr(client, "calls", 0)
        wf.tier_stats[name]["vendor_hits"] = getattr(client, "hits", 0)

    # Per-tier breakdown: attempts / hits / rough cost (vendor pricing opaque → 0).
    tier_breakdown = {}
    for tier_name, stats in wf.tier_stats.items():
        allowed = tier_allowed(
            "aiark" if tier_name == "ai_ark" else tier_name, max_tier_n
        ) if tier_name in TIER_RANK or tier_name == "ai_ark" else True
        tier_breakdown[tier_name] = {
            "attempts": int(stats.get("calls") or 0),
            "email_hits": int(stats.get("email_hits") or 0),
            "dm_hits": int(stats.get("dm_hits") or 0),
            "vendor_calls": int(stats.get("vendor_calls") or 0),
            "vendor_hits": int(stats.get("vendor_hits") or 0),
            "skip_count": int(stats.get("skip_count") or 0),
            "skips": list(stats.get("skips") or [])[:20],
            "last_skip_reason": stats.get("last_skip_reason"),
            "allowed_by_max_tier": allowed,
            "estimated_cost_usd": 0.0,
        }

    return {
        "rows_in": len(parsed),
        "companies_upserted": companies_upserted,
        "contacts_written": contacts_written,
        "emails_found": emails_found,
        "dms_found": dms_found,
        "tier_stats": wf.tier_stats,
        "tier_breakdown": tier_breakdown,
        "need": need,
        "max_tier": max_tier_n,
        "target_titles": titles,
        "require_title_match": bool(require_title_match),
        "client_tag": write_target.get("client_tag") or None,
        "supabase_schema": write_target.get("schema"),
        "apify": apify_result or None,
        "vendors_enabled": {
            "apify": bool(settings.apify_token) and wf._allowed("apify"),
            "getleads": wf.getleads.enabled and wf._allowed("getleads"),
            "ai_ark": wf.ai_ark.enabled and wf._allowed("aiark"),
            "leadmagic": wf.leadmagic.enabled and wf._allowed("leadmagic"),
            "fullenrich": wf.fullenrich.enabled and wf._allowed("fullenrich"),
        },
    }
