# googlemaps-scraper

Build a US local-business lead list off Google Maps, then qualify it with a
local LLM so you pay nothing for the cleaning step.

The businesses worth reaching here — funeral homes, HVAC shops, gyms, dentists
— mostly have no LinkedIn presence, so LinkedIn-derived databases don't have
them. Google Maps does.

```
zips ──► scrape ──► enrich ──► classify ──► owners ──► export
 │         │          │           │            │          │
offline  RapidAPI  html2text   Gemma on     Gemma on    CSV
         (paid)     (free)     Ollama       Ollama +
                               (free)       web (cheap)
```

Every stage checkpoints into one SQLite file. Kill any stage at any point and
re-run the same command — it picks up exactly where it stopped and never
re-spends on work already done.

---

## Setup

**1. Ollama** (you already have it) — pull the model:

```bash
ollama pull gemma4:12b     # ~8 GB, needs ~16 GB RAM/VRAM
ollama serve               # if it isn't already running
```

**2. This repo:**

```bash
git clone https://github.com/joshuaosborn561-lang/googlemaps-scraper
cd googlemaps-scraper
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

**3. Maps Data key** — subscribe to
[Maps Data on RapidAPI](https://rapidapi.com/alexanderxbx/api/maps-data),
copy your key into `RAPIDAPI_KEY` in `.env`.

**4. (Optional) OpenWeb Ninja key** for the owner-name web fallback →
`OPENWEBNINJA_KEY`. Leave it blank to skip that step; everything else works
without it.

---

## Run it

```bash
# 0. Build the ZIP list (offline, no API calls, ~2s)
python -m gmscraper zips
#    -> 29,673 ZIP codes -> data/us_zipcodes.csv

# 1. See what a run costs BEFORE spending anything
python -m gmscraper estimate --vertical funeral
#    Requests: 356,076   Cost: $11.87

# 2. Verify the API actually returns what you expect — one request
python -m gmscraper probe --zip 01001 --category "funeral home"

# 3. Scrape. Start with one state to sanity-check the data.
python -m gmscraper scrape --vertical funeral --states OH --workers 8
#    ...then go national by dropping --states
python -m gmscraper scrape --vertical funeral --workers 8

# 4. Pull website text (free, no API)
python -m gmscraper enrich --workers 12

# 5. Local Gemma confirms each business fits the ICP (free)
python -m gmscraper classify --vertical funeral --workers 2

# 6. Local Gemma finds the owner's name (free; --fallback adds paid web search)
python -m gmscraper owners --fallback --workers 2

# 7. CSV out
python -m gmscraper export --out out/funeral_homes.csv --with-phone
python -m gmscraper stats
```

`python -m gmscraper <command> --help` documents every flag.

---

## Step 2 is the one not to skip

RapidAPI's docs sit behind a login and providers rename response fields
without warning. `probe` makes one request and shows you the raw JSON next to
the normalized row, then tells you which fields came out empty:

```
Empty after mapping: city, state, zip
If those exist in the raw item above under another name, add that name to
ALIASES in gmscraper/mapsdata.py and re-run probe.
```

Two things make that cheap to recover from:

* Every scraped row keeps the provider's untouched JSON in `raw_json`. After
  editing `ALIASES`, run `python -m gmscraper renormalize` to re-map every row
  already on disk — **zero API calls**.
* If the provider changes the endpoint path, set `MAPS_DATA_PATH` in `.env`
  and pass extra query params with `--param key=value`. No code change.

So a wrong guess about the schema costs you a re-run of `renormalize`, not a
re-run of a 356,000-request scrape.

---

## Categories: the part that decides how good the list is

A business hides under more categories than the obvious one. A funeral home is
also a *memorial park*, a *mortuary*, a *cremation service*. Search only
"funeral home" and you miss a large share of the market.

`config/categories.yml` ships ten verticals with the aliases already expanded
(funeral, hvac, gyms, home_services, dental, restaurants, auto, med_spa,
legal, veterinary). Each block also carries an `icp:` sentence, which is fed
verbatim to the local model in step 5 — write it the way you'd brief a
teammate, exclusions included:

```yaml
funeral:
  icp: >
    An independently owned funeral home, mortuary, crematory or cemetery that
    serves families directly. Exclude casket/urn e-commerce stores, headstone
    manufacturers, life-insurance agencies, hospices and pet cremation.
  categories:
    - funeral home
    - mortuary
    - cremation service
    - memorial park
    ...
```

```bash
python -m gmscraper categories                      # list what's available
python -m gmscraper scrape --categories "gym,crossfit box,yoga studio" \
                           --icp "Independently owned fitness studios"
```

Cost is linear in category count, so adding aliases is the main lever on both
coverage and spend. Run `estimate` after editing.

---

## What it actually costs

At the quoted $100 / 3M requests (`PRICE_PER_REQUEST` in `.env`), with the
default 29,673-ZIP list:

| Vertical | Categories | Requests | Cost |
|---|---:|---:|---:|
| funeral | 12 | 356,076 | $11.87 |
| hvac | 14 | 415,422 | $13.85 |
| home_services | 20 | 593,460 | $19.78 |
| one category | 1 | 29,673 | $0.99 |

Steps 4–6 are free — html2text is open source and Gemma runs on your own
machine. OpenWeb Ninja in step 6 is the only other paid piece, and only fires
for businesses where the website came up empty.

Two notes on the numbers in the post you sent:

* **"$19 a category"** doesn't reconcile with "$100 for 3M requests" — at that
  rate one category nationwide is about **$1**, and $19 is roughly a *20-category
  vertical*. That matches the post's other line ("one vertical of 20 categories
  ≈ $100" is also high). Either way, `estimate` prints your real number from
  your real plan price, so set `PRICE_PER_REQUEST` and trust that.
* **"42,734 ZIP codes"** counts every ZIP type. The default list here is
  29,673 — active, standard, 50 states + DC. PO Box, military and territory
  ZIPs are radius-searched from inside a standard ZIP's footprint anyway, so
  they return duplicates you pay for and dedup throws away. `--types all
  --territories` if you want them.

---

## Throughput

**Scrape** is network-bound; 8–16 workers is the useful range. A 356k-request
vertical is a few hours. The `scrape` progress line shows rate, ETA and running
spend.

**Classify/owners** are bound by your GPU. Gemma 4 12B does roughly 2–6
businesses/second on a recent 16 GB card, well under 1/s on CPU only. Start
with `--workers 2` and raise it until throughput stops improving. Two ways to
cut the bill if the list is large:

```bash
# only classify what you'll actually mail: businesses with a website
python -m gmscraper classify --vertical funeral --workers 4

# owners only for confirmed-ICP rows (this is the default)
python -m gmscraper owners --fallback --workers 4
```

`--limit N` on any LLM stage runs a sample first so you can eyeball the
verdicts before committing the whole list.

Sanity-check the model's judgement early:

```sql
sqlite3 leads.db "SELECT b.name, v.in_icp, v.confidence, v.reason
                  FROM verdicts v JOIN businesses b USING(place_id) LIMIT 20;"
```

If it's too loose, tighten the `icp:` exclusions or raise `--min-confidence`.

---

## Export

```bash
python -m gmscraper export --out out/leads.csv                  # in-ICP only
python -m gmscraper export --out out/leads.csv --with-owner     # named owner only
python -m gmscraper export --out out/oh.csv --states OH PA MI
python -m gmscraper export --out out/all.csv --all              # everything
python -m gmscraper export --out out/leads.csv --min-confidence 0.8
```

Columns: `place_id, name, owner_name, owner_title, owner_source, phone,
website, domain, address, city, state, zip, rating, reviews, main_category,
types, latitude, longitude, maps_url, in_icp, icp_confidence, icp_reason,
source_category`.

`owner_source` is `website`, `websearch` or `none` — useful for deciding how
much to trust a first name before you merge it into a mail-merge.

---

## Design notes

**Dedup.** Neighbouring ZIPs return heavily overlapping results. Businesses are
keyed on the provider's place id, so 20 ZIPs surfacing the same funeral home
store one row, not 20.

**Website fetching is per-domain, not per-business.** A chain with 30
locations is fetched and read once.

**The model is told to return null rather than guess.** A wrong first name in
a cold email is worse than no first name.

**Robots.txt is respected by default** when fetching business websites
(`--ignore-robots` to override, `--delay` to slow down per-domain requests).
Aggregator "websites" (Facebook, Yelp, DoorDash, Linktree…) are skipped —
they're not the business's own site, so there's nothing on them worth reading.

---

## Tests

```bash
pip install pytest && python -m pytest tests/ -q     # 17 tests, no network, no API key
```

Covers response normalization across differing field names, address parsing,
aggregator rejection, cross-ZIP dedup, job checkpoint/resume and export
filtering.

---

## Caveats

* The Maps Data endpoint schema is unverified against a live key — I couldn't
  reach RapidAPI from the machine this was built on. Run `probe` first; that
  step exists precisely to catch it, and `renormalize` fixes any mismatch
  without re-scraping.
* Capping at 20 results per ZIP (the provider's first page) is what keeps cost
  linear. In a dense urban ZIP with more than 20 matching businesses you will
  miss the tail; the overlap from neighbouring ZIPs recovers much but not all
  of it. Raise `--limit-results` if your provider plan returns more per call.
* Scraped business data is factual listing information, but how you *use* it is
  on you — CAN-SPAM, state privacy law and the provider's own terms all apply
  to the outreach, not just the collection.
