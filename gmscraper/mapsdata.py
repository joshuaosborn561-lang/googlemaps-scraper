"""Maps Data (RapidAPI) client.

The provider's field names are not contractually stable, and RapidAPI's docs
sit behind a login, so this module does not hard-code a response shape.  It

  1. sends a request whose parameter names you can override from the CLI,
  2. finds the result list wherever it happens to sit in the envelope,
  3. maps fields through an alias table that is matched case- and
     underscore-insensitively, and
  4. keeps the provider's untouched JSON on every row.

If a field lands empty, run `probe` to see the real payload, add the key to
ALIASES, and run `renormalize` -- no re-scraping and no extra API spend.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

import requests

from .config import Settings

# Result rows may arrive under any of these envelope keys, or as a bare list.
LIST_KEYS = ("data", "results", "items", "places", "businesses", "result")

ALIASES: dict[str, tuple[str, ...]] = {
    "place_id": (
        "business_id", "place_id", "placeid", "id", "cid", "data_id",
        "google_id", "fid", "data_cid",
    ),
    "name": ("name", "title", "business_name", "displayname"),
    "address": (
        "full_address", "address", "formatted_address", "addr", "vicinity",
        "street_address",
    ),
    "city": ("city", "locality", "town"),
    "state": ("state", "region", "administrative_area", "us_state"),
    "zip": ("zipcode", "zip", "postal_code", "postcode"),
    "phone": ("phone_number", "phone", "formatted_phone_number", "telephone", "tel"),
    "website": ("website", "site", "url", "web", "domain_url"),
    "rating": ("rating", "average_rating", "stars", "score"),
    "reviews": ("review_count", "reviews", "user_ratings_total", "reviews_count",
                "num_reviews", "rating_count"),
    "main_category": ("type", "category", "main_category", "primary_type",
                      "business_type"),
    "types": ("types", "categories", "subtypes", "type_list"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lng", "lon", "long"),
    "maps_url": ("place_link", "maps_url", "google_maps_url", "link", "place_url",
                 "gmaps_url"),
}

# Directory/aggregator hosts. A business "website" pointing here is not the
# business's own site, so there is nothing on it worth scraping or reading.
AGGREGATORS = {
    "facebook.com", "m.facebook.com", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "yelp.com", "yellowpages.com", "tripadvisor.com",
    "google.com", "sites.google.com", "business.site", "wixsite.com",
    "square.site", "linktr.ee", "doordash.com", "grubhub.com", "ubereats.com",
    "opentable.com", "booksy.com", "vagaro.com", "toasttab.com",
    "angi.com", "homeadvisor.com", "thumbtack.com", "bbb.org", "mapquest.com",
    "nextdoor.com", "tiktok.com", "youtube.com", "amazon.com", "ebay.com",
}


class MapsDataError(RuntimeError):
    pass


class AuthError(MapsDataError):
    """Bad key or no active subscription -- retrying will not help."""


def _key(s: str) -> str:
    return s.replace("_", "").replace("-", "").replace(" ", "").lower()


def _flatten(obj: Any, depth: int = 2) -> dict[str, Any]:
    """Collapse a nested dict into normalized-key -> value, outer wins."""
    out: dict[str, Any] = {}

    def walk(node: Any, d: int) -> None:
        if not isinstance(node, dict) or d < 0:
            return
        for k, v in node.items():
            nk = _key(str(k))
            if nk not in out and v not in (None, "", [], {}):
                out[nk] = v
        for v in node.values():
            if isinstance(v, dict):
                walk(v, d - 1)

    walk(obj, depth)
    return out


def _pick(flat: dict[str, Any], names: Sequence[str]) -> Any:
    for n in names:
        v = flat.get(_key(n))
        if v not in (None, "", [], {}):
            return v
    return None


def _as_float(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def domain_of(url: str | None) -> str:
    """Registrable host of a business website, or '' if it is an aggregator."""
    if not url:
        return ""
    url = str(url).strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return ""
    # Treat any subdomain of an aggregator as an aggregator too.
    parts = host.split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in AGGREGATORS:
            return ""
    return host


# "12 River Rd, Agawam, MA 01001, USA" -> ("Agawam", "MA", "01001")
US_ADDR = re.compile(
    r",\s*([^,]+?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*(?:,\s*(?:USA|US|United States)\s*)?$",
    re.I,
)
# Weaker fallback when the city is not comma-delimited.
US_STATE_ZIP = re.compile(r"\b([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b")


def parse_address_parts(address: str) -> tuple[str, str, str]:
    """Best-effort (city, state, zip) from a formatted US address.

    Providers vary on whether they break the address out into components. When
    they do not, `state` still has to be populated or `--states` filtering and
    per-state exports silently return nothing.
    """
    if not address:
        return "", "", ""
    m = US_ADDR.search(address.strip())
    if m:
        return m.group(1).strip(), m.group(2).upper(), m.group(3)
    m = US_STATE_ZIP.search(address)
    if m:
        return "", m.group(1).upper(), m.group(2)
    return "", "", ""


def extract_list(payload: Any) -> list[dict[str, Any]]:
    """Find the result array inside whatever envelope came back."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for k in LIST_KEYS:
        v = payload.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):  # e.g. {"data": {"results": [...]}}
            inner = extract_list(v)
            if inner:
                return inner
    # Last resort: the longest list-of-dicts anywhere at the top level.
    best: list[dict[str, Any]] = []
    for v in payload.values():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            if len(v) > len(best):
                best = v
    return best


def normalize(
    item: dict[str, Any], source_zip: str = "", source_category: str = ""
) -> dict[str, Any]:
    flat = _flatten(item)
    rec: dict[str, Any] = {}
    for field, names in ALIASES.items():
        rec[field] = _pick(flat, names)

    rec["rating"] = _as_float(rec["rating"])
    rec["reviews"] = _as_int(rec["reviews"])
    rec["latitude"] = _as_float(rec["latitude"])
    rec["longitude"] = _as_float(rec["longitude"])

    types = rec.get("types")
    if isinstance(types, str):
        types = [t.strip() for t in types.split(",") if t.strip()]
    elif isinstance(types, list):
        types = [str(t).strip() for t in types if str(t).strip()]
    else:
        types = []
    rec["types"] = types
    if not rec.get("main_category") and types:
        rec["main_category"] = types[0]

    for f in ("place_id", "name", "address", "city", "state", "zip", "phone",
              "website", "main_category", "maps_url"):
        rec[f] = str(rec[f]).strip() if rec[f] is not None else ""

    # Fill any component the provider did not break out itself.
    if not (rec["city"] and rec["state"] and rec["zip"]):
        city, state, zipc = parse_address_parts(rec["address"])
        rec["city"] = rec["city"] or city
        rec["state"] = rec["state"] or state
        rec["zip"] = rec["zip"] or zipc
    # Last resort: the ZIP we searched. Close enough for routing, and only
    # used when the payload carried nothing better.
    if not rec["zip"] and source_zip:
        rec["zip"] = source_zip

    rec["state"] = rec["state"].upper() if len(rec["state"]) == 2 else rec["state"]
    rec["domain"] = domain_of(rec["website"])

    # No stable id from the provider? Fall back to something deterministic so
    # dedup across overlapping ZIPs still works.
    if not rec["place_id"]:
        seed = f"{rec['name']}|{rec['address']}|{rec['phone']}".strip("|")
        rec["place_id"] = f"syn:{abs(hash(seed)):016x}" if seed else ""

    rec["source_zip"] = source_zip
    rec["source_category"] = source_category
    rec["raw"] = item
    return rec


class MapsDataClient:
    def __init__(
        self,
        settings: Settings,
        limit: int = 20,
        query_template: str = "{category} in {zip}",
        extra_params: dict[str, str] | None = None,
        timeout: int = 30,
        max_retries: int = 4,
    ):
        self.s = settings
        self.limit = limit
        self.query_template = query_template
        self.extra_params = extra_params or {}
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-rapidapi-key": settings.rapidapi_key,
                "x-rapidapi-host": settings.maps_host,
                "Accept": "application/json",
            }
        )
        self.request_count = 0

    def build_params(self, category: str, zip_row: dict[str, str]) -> dict[str, str]:
        q = self.query_template.format(
            category=category,
            zip=zip_row.get("zip", ""),
            city=zip_row.get("city", ""),
            state=zip_row.get("state", ""),
        )
        params = {
            "query": q,
            "limit": str(self.limit),
            "country": "us",
            "lang": "en",
            "offset": "0",
            "zoom": "13",
        }
        # Geo hints sharpen the search; the ZIP is also in the query text so
        # results stay correct even if the provider ignores lat/lng.
        if zip_row.get("lat") and zip_row.get("lng"):
            params["lat"] = str(zip_row["lat"])
            params["lng"] = str(zip_row["lng"])
        params.update(self.extra_params)
        return params

    def raw_search(self, category: str, zip_row: dict[str, str]) -> Any:
        params = self.build_params(category, zip_row)
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self.request_count += 1
                r = self.session.get(self.s.maps_url, params=params, timeout=self.timeout)
                if r.status_code in (401, 403):
                    raise AuthError(
                        f"HTTP {r.status_code} from {self.s.maps_host}: check "
                        f"RAPIDAPI_KEY and that you are subscribed to the API. "
                        f"Body: {r.text[:200]}"
                    )
                if r.status_code == 429:
                    wait = float(r.headers.get("Retry-After") or 0) or (2 ** attempt)
                    time.sleep(min(wait, 60) + random.uniform(0, 1))
                    last = MapsDataError("429 rate limited")
                    continue
                if r.status_code >= 500:
                    last = MapsDataError(f"HTTP {r.status_code}")
                    time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                    continue
                r.raise_for_status()
                return r.json()
            except AuthError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last = exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
        raise MapsDataError(f"failed after {self.max_retries + 1} attempts: {last}")

    def search(
        self, category: str, zip_row: dict[str, str]
    ) -> list[dict[str, Any]]:
        payload = self.raw_search(category, zip_row)
        items = extract_list(payload)
        out = []
        for it in items[: self.limit]:
            rec = normalize(it, zip_row.get("zip", ""), category)
            if rec["place_id"]:
                out.append(rec)
        return out


def renormalize(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-run normalization over stored raw JSON (after editing ALIASES)."""
    return [normalize(r["raw"], r.get("source_zip", ""), r.get("source_category", ""))
            for r in rows]
