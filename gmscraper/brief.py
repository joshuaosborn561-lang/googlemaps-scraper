"""Natural language in, run plan out.

    "find me independent HVAC companies in Ohio and Michigan with at
     least 20 reviews and 4+ stars, I need the owner's name and email"

becomes a concrete plan: the Google Maps categories to search, the ICP
sentence the classifier will judge against, and the export filters.

The expansion runs on your local Gemma, so it is free and you can re-plan as
many times as you like before spending anything on the scrape. The plan is
printed for you to edit and approve -- it is never executed blind, because
category choice is what drives both cost and list quality.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from typing import Any

from .llm import Ollama
from .zips import STATES_50

SYSTEM = (
    "You turn a sales prospecting brief into a Google Maps scraping plan. "
    "You know how Google Maps categorises local businesses, including that "
    "one business is often listed under several different category names "
    "(a funeral home is also a 'memorial park', a 'mortuary' and a "
    "'cremation service'; a gym is also a 'fitness center' and a "
    "'personal trainer'). Your job is to list EVERY Maps category the target "
    "business might be filed under, because any category you omit is a "
    "segment of the market that will be missing from the list. "
    "Honor negative constraints literally: if the brief says not to include a "
    "business type, put it in exclude_categories and never in categories. "
    "When the brief expresses a radius around a city ('within 150 miles of "
    "Dallas'), set center and radius_miles and put ONLY the home state of that "
    "city in states — do NOT widen to neighboring states."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "vertical": {"type": "string"},
        "categories": {"type": "array", "items": {"type": "string"}},
        "exclude_categories": {"type": "array", "items": {"type": "string"}},
        "icp": {"type": "string"},
        "states": {"type": "array", "items": {"type": "string"}},
        "center": {"type": "string"},
        "radius_miles": {"type": "number"},
        "min_rating": {"type": "number"},
        "min_reviews": {"type": "integer"},
        "require_website": {"type": "boolean"},
        "require_phone": {"type": "boolean"},
        "require_email": {"type": "boolean"},
        "require_owner": {"type": "boolean"},
    },
    "required": [
        "vertical", "categories", "exclude_categories", "icp", "states",
        "center", "radius_miles",
        "min_rating", "min_reviews",
        "require_website", "require_phone", "require_email", "require_owner",
    ],
}

PROMPT = """BRIEF:
{brief}

Produce the scraping plan as JSON.

  vertical            short snake_case name for this list
  categories          Google Maps category strings to search, lowercase.
                      Include every category the TARGET might be listed under,
                      including non-obvious synonyms. 8-20 is typical.
                      NEVER include a category the brief says to skip / exclude /
                      "do not include". Those go in exclude_categories only.
  exclude_categories  Maps categories the brief explicitly does not want
                      (e.g. brief says "do not include roofing contractors"
                      → ["roofing contractor"]). Empty array if none.
  icp                 2-3 sentences describing exactly who qualifies, written as
                      a brief to a colleague. MUST end with an explicit "Exclude
                      ..." sentence listing adjacent business types that will
                      show up but are not wanted.
  states              two-letter US state codes for the region.
                      If the brief gives a radius around a city, put ONLY that
                      city's state here (Dallas → ["TX"], not ["TX","OK"]).
                      Empty array means the whole United States.
  center              If the brief expresses a radius / "within N miles of X",
                      set this to "City, ST" (e.g. "Dallas, TX"). Otherwise "".
  radius_miles        Miles from center when the brief gives a radius
                      (e.g. 150). 0 if the brief does not express a radius.
  min_rating          minimum Google rating, 0 if the brief does not say
  min_reviews         minimum review count, 0 if the brief does not say
  require_website     true if the brief implies they must have a website
  require_phone       true if a phone number is required
  require_email       true if an email address is required
  require_owner       true if the owner's name is required
"""


_GENERIC_CAT_TOKENS = frozenset(
    {
        "contractor",
        "contractors",
        "company",
        "companies",
        "service",
        "services",
        "firm",
        "firms",
        "shop",
        "shops",
        "center",
        "the",
        "and",
        "of",
    }
)


def _norm_cat(c: str) -> str:
    return " ".join(str(c).lower().split()).strip(" .,")


def _parse_exclude_list(text: str | None) -> list[str]:
    if not text:
        return []
    parts = re.split(r"[,;\n]+", text)
    out, seen = [], set()
    for p in parts:
        c = _norm_cat(p)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _category_excluded(category: str, excludes: set[str]) -> bool:
    """True if category matches an exclude exactly, by substring, or by core token."""
    if category in excludes:
        return True
    for ex in excludes:
        if ex in category or category in ex:
            return True
        cores = [t for t in ex.split() if t not in _GENERIC_CAT_TOKENS and len(t) > 2]
        if cores and any(re.search(rf"\b{re.escape(tok)}\b", category) for tok in cores):
            return True
    return False


@dataclass
class Plan:
    vertical: str = "custom"
    categories: list[str] = field(default_factory=list)
    exclude_categories: list[str] = field(default_factory=list)
    icp: str = ""
    states: list[str] = field(default_factory=list)
    # Geography targeting (optional). Precedence at runtime:
    # plan.zips > center+radius_miles > states.
    zips: list[str] = field(default_factory=list)
    center: str = ""
    center_lat: float | None = None
    center_lng: float | None = None
    radius_miles: float = 0.0
    min_rating: float = 0.0
    min_reviews: int = 0
    require_website: bool = False
    require_phone: bool = False
    require_email: bool = False
    require_owner: bool = False

    @classmethod
    def from_model(cls, raw: dict[str, Any]) -> "Plan":
        cats, seen = [], set()
        for c in raw.get("categories") or []:
            c = _norm_cat(c)
            if c and c not in seen:
                seen.add(c)
                cats.append(c)

        excludes, ex_seen = [], set()
        for c in raw.get("exclude_categories") or []:
            c = _norm_cat(c)
            if c and c not in ex_seen:
                ex_seen.add(c)
                excludes.append(c)

        # Drop any category that was also excluded (belt + suspenders).
        if excludes:
            ex_set = set(excludes)
            cats = [c for c in cats if not _category_excluded(c, ex_set)]

        states = []
        for s in raw.get("states") or []:
            s = str(s).strip().upper()
            if s in STATES_50 and s not in states:
                states.append(s)

        def num(key, cast, default):
            try:
                val = raw.get(key)
                if val is None or val == "":
                    return default
                return cast(val)
            except (TypeError, ValueError):
                return default

        center = " ".join(str(raw.get("center") or "").split())
        radius = num("radius_miles", float, 0.0)
        if radius < 0:
            radius = 0.0
        # If radius is set but center empty, clear radius; if center set with 0, keep center.
        if not center:
            radius = 0.0

        # Radius briefs must not silently widen to neighboring states.
        if center and radius > 0 and len(states) > 1:
            # Prefer the state mentioned in the center string.
            m = re.search(r"\b([A-Z]{2})\b", center.upper())
            if m and m.group(1) in STATES_50:
                states = [m.group(1)]
            else:
                states = states[:1]

        return cls(
            vertical="".join(
                ch if ch.isalnum() else "_" for ch in str(raw.get("vertical") or "custom").lower()
            ).strip("_") or "custom",
            categories=cats,
            exclude_categories=excludes,
            icp=" ".join(str(raw.get("icp") or "").split()),
            states=states,
            center=center,
            radius_miles=radius,
            min_rating=num("min_rating", float, 0.0),
            min_reviews=num("min_reviews", int, 0),
            require_website=bool(raw.get("require_website")),
            require_phone=bool(raw.get("require_phone")),
            require_email=bool(raw.get("require_email")),
            require_owner=bool(raw.get("require_owner")),
        )

    def apply_exclusions(self, extra: list[str] | None = None) -> None:
        """Remove excluded categories from the scrape list."""
        excludes = list(self.exclude_categories)
        if extra:
            for c in extra:
                c = _norm_cat(c)
                if c and c not in excludes:
                    excludes.append(c)
        self.exclude_categories = excludes
        if not excludes:
            return
        ex_set = set(excludes)
        self.categories = [
            c for c in self.categories if not _category_excluded(c, ex_set)
        ]

    def to_yaml_block(self) -> str:
        lines = [f"{self.vertical}:", "  icp: >"]
        words, line = self.icp.split(), ""
        for w in words:
            if len(line) + len(w) > 70:
                lines.append(f"    {line}")
                line = w
            else:
                line = f"{line} {w}".strip()
        if line:
            lines.append(f"    {line}")
        lines.append("  categories:")
        lines += [f"    - {c}" for c in self.categories]
        return "\n".join(lines)

    def describe(self, n_zips: int, plan=None, used_this_month: int = 0) -> str:
        n = n_zips * len(self.categories)
        req = [
            k for k, v in (
                ("website", self.require_website), ("phone", self.require_phone),
                ("email", self.require_email), ("owner name", self.require_owner),
            ) if v
        ]
        if self.zips:
            region = f"explicit {len(self.zips)} ZIPs"
        elif self.center and self.radius_miles:
            region = f"{self.radius_miles:g} mi of {self.center}"
            if self.states:
                region += f" ({', '.join(self.states)})"
        else:
            region = ", ".join(self.states) if self.states else "entire United States"
        out = [
            f"  vertical    {self.vertical}",
            f"  categories  {len(self.categories)}: {', '.join(self.categories)}",
            f"  ICP         {self.icp}",
            f"  region      {region}",
            f"  zips        {n_zips:,}",
        ]
        if self.exclude_categories:
            out.append(
                f"  excluded    {', '.join(self.exclude_categories)}"
            )
        if self.min_rating or self.min_reviews:
            out.append(
                f"  quality     rating >= {self.min_rating or 0}, "
                f"reviews >= {self.min_reviews or 0}"
            )
        if req:
            out.append(f"  must have   {', '.join(req)}")
        out.append(f"  requests    {n:,}")
        out += cost_lines(n, plan, used_this_month)
        return "\n".join(out)


def cost_lines(n: int, plan=None, used_this_month: int = 0) -> list[str]:
    """Price a run against the subscription tier, not a flat per-request rate."""
    if plan is None:
        return [f"  est. cost   {n:,} requests (no plan configured)"]

    left = max(0, plan.included - used_this_month)
    cost, billable = plan.cost_for(n, used_this_month)
    out = [
        f"  plan        {plan.name} — ${plan.monthly_usd:,.0f}/mo, "
        f"{plan.included:,} requests included",
        f"  quota left  {left:,} of {plan.included:,} "
        f"({used_this_month:,} used this cycle)",
    ]
    if billable == 0:
        out.append(f"  est. cost   $0.00 extra — fits inside this cycle's quota")
    elif cost == float("inf"):
        out.append(
            f"  est. cost   BLOCKED — {billable:,} requests over a hard-limit "
            f"plan. Upgrade before running."
        )
    else:
        out.append(
            f"  est. cost   ${cost:,.2f} overage "
            f"({billable:,} x ${plan.overage_usd:g})"
        )
        out.append(f"  cycle total ${plan.monthly_usd + cost:,.2f} including the plan fee")

    from .config import cheapest_plan

    best, best_total = cheapest_plan(n, used_this_month)
    mine = plan.monthly_usd + (0.0 if cost == float("inf") else cost)
    if best.name != plan.name and best_total < mine:
        out.append(
            f"  cheaper on  {best.name} — ${best_total:,.2f} this cycle "
            f"(saves ${mine - best_total:,.2f})"
        )
    out.append("  (enrich / classify / owners run locally and are free)")
    return out


def make_plan(ollama: Ollama, brief: str) -> Plan:
    raw = ollama.json_chat(SYSTEM, PROMPT.format(brief=brief.strip()), SCHEMA)
    plan = Plan.from_model(raw)
    if not plan.categories:
        raise SystemExit(
            "The model returned no categories. Re-run with a more specific "
            "brief, or use --categories to name them yourself."
        )
    return plan


def save(plan: Plan, path: str) -> None:
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(plan.__dict__, indent=2), encoding="utf-8")


def load(path: str) -> Plan:
    from pathlib import Path

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    known = {f.name for f in fields(Plan)}
    return Plan(**{k: v for k, v in data.items() if k in known})
