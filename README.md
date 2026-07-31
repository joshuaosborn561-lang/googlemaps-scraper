# googlemaps-scraper

Build a US local-business lead list off Google Maps, then qualify it with a
local LLM so you pay nothing for the cleaning step.

The businesses worth reaching here — funeral homes, HVAC shops, gyms, dentists
— mostly have no LinkedIn presence, so LinkedIn-derived databases don't have
them. Google Maps does.

```
       "independent HVAC shops in Ohio, 4+ stars, owner name and email"
                              │
                        plan (local Gemma, free)
                              ▼
zips ──► scrape ──► enrich ──────► classify ──► owners ──► export
 │         │          │              │            │          │
offline  RapidAPI  html2text      Gemma on     Gemma on    CSV
         (paid)    + emails       Ollama       Ollama +
                    (free)        (free)       web (cheap)
```

Every stage checkpoints into one SQLite file. Kill any stage at any point and
re-run the same command — it picks up exactly where it stopped and never
re-spends on work already done.

---

## Setup

**1. Ollama** (you already have it) — pull the model:

```bash
ollama pull gemma4:e4b     # ~4.7 GB, runs fine on CPU
ollama serve               # if it isn't already running
```

Which model depends on your hardware, and it matters a lot — measure rather
than guess:

```bash
python -m gmscraper bench
```

It runs the real classify prompt against each candidate and reports prefill
and generation speed separately, then projects a full run:

| Hardware | Model | Notes |
|---|---|---|
| GPU, 12 GB+ VRAM | `gemma4:12b` | best quality |
| CPU only, 16 GB RAM | `gemma4:e4b` | the edge-sized Gemma, ~4.7 GB |
| CPU only, slow box | `gemma4:e2b` | ~3.1 GB, fastest |
| alternatives | `qwen3.5:4b`, `phi4-mini` | |

A 12B on a CPU-only laptop is **minutes** per business, not seconds. Ollama
does not use the NPU on Copilot+ / Snapdragon machines — it runs on CPU.

**2. This repo:**

```bash
git clone -b claude/google-maps-scraping-3v0uqa \
    https://github.com/joshuaosborn561-lang/googlemaps-scraper
cd googlemaps-scraper
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

**3. Maps Data key** — subscribe to
[Maps Data on RapidAPI](https://rapidapi.com/alexanderxbx/api/maps-data),
copy your key into `RAPIDAPI_KEY` in `.env`.

**4. (Optional) Apify token** for the owner-name web fallback →
`APIFY_TOKEN`, from [Apify console](https://console.apify.com/settings/integrations).
Leave it blank to skip that step; everything else works without it. It uses
ScraperLink's Google SERP actor at $0.0005 per lookup, and Apify's free tier
($5/month of credits) covers about 10,000 — so in practice this stage is free
at the volumes here.

---

## Or just talk to Claude Code

If you'd rather not learn the flags, run [Claude Code](https://code.claude.com/docs/en/quickstart)
inside this repo and describe what you want:

```bash
# install once - Windows PowerShell:
irm https://claude.ai/install.ps1 | iex
# macOS / Linux / WSL:
curl -fsSL https://claude.ai/install.sh | bash

cd googlemaps-scraper
claude
```

Then: *"get me independent HVAC companies in Ohio with owner names and emails."*

`CLAUDE.md` in this repo tells it which commands to run, to show you the cost
estimate and wait for approval before anything paid, to pilot one state before
going national, and to never delete `leads.db`. It runs on your machine, so it
can reach your Ollama and your RapidAPI key directly.

---

## Just describe what you want

```bash
python -m gmscraper plan "independent HVAC companies in Ohio and Michigan, \
    4+ stars and at least 20 reviews, I need the owner's name and an email"
```

Your local Gemma expands that into a plan and prices it before you spend
anything:

```
PLAN
  vertical    hvac_contractors
  categories  13: hvac contractor, heating contractor, air conditioning
              contractor, furnace repair service, air conditioning repair
              service, heat pump supplier, boiler supplier, ...
  ICP         Independent residential and light-commercial HVAC contractors.
              Exclude equipment manufacturers, parts wholesalers, big-box
              retailers and national franchise call centers.
  region      OH, MI
  zips        1,916
  quality     rating >= 4.0, reviews >= 20
  must have   phone, email, owner name
  requests    24,908
  plan        ultra — $25/mo, 300,000 requests included
  quota left  300,000 of 300,000 (0 used this cycle)
  est. cost   $0.00 extra — fits inside this cycle's quota
  (enrich / classify / owners run locally and are free)
```

Happy with it? Run the whole thing — scrape, website fetch, ICP filter, owner
lookup, CSV — with one command:

```bash
python -m gmscraper run "independent HVAC companies in Ohio and Michigan, \
    4+ stars and 20+ reviews, owner name and email" --out out/hvac.csv
```

It prints the same plan, asks you to confirm, then drives all five stages.
`--yes` skips the prompt.

Planning is free and runs entirely on your machine, so iterate on the wording
until the category list looks right. To hand-edit before running:

```bash
python -m gmscraper plan "..." --save plans/hvac.json
$EDITOR plans/hvac.json
python -m gmscraper run --plan plans/hvac.json --out out/hvac.csv
```

The category list is the single biggest driver of both cost and list quality,
which is why `run` always shows it and never executes blind.

The stages below are the same thing with the lid off — use them when you want
to re-run one part (say, re-classify against a tighter ICP) without redoing
the scrape.

---

## Run it stage by stage

```bash
# 0. Build the ZIP list (offline, no API calls, ~2s)
python -m gmscraper zips
#    -> 29,673 ZIP codes -> data/us_zipcodes.csv

# 1. See what a run costs BEFORE spending anything
python -m gmscraper estimate --vertical funeral
#    Requests: 356,076   ultra: $50.47 overage, $75.47 for the month

# 2. Verify the API actually returns what you expect — one request
python -m gmscraper probe --zip 01001 --category "funeral home"

# 3. Scrape. Start with one state to sanity-check the data.
python -m gmscraper scrape --vertical funeral --states OH --workers 8
#    ...then go national by dropping --states
python -m gmscraper scrape --vertical funeral --workers 8

# 4. Pull website text (free, no API)
python -m gmscraper enrich --workers 12

# 5. Local Gemma confirms each business fits the ICP (free)
python -m gmscraper classify --vertical funeral --workers 1

# 6. Local Gemma finds the owner's name (free; --fallback adds paid web search)
python -m gmscraper owners --fallback --workers 1

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

Maps Data bills as a monthly plan with an included request quota, then
per-request overage. Set `MAPS_PLAN` in `.env` and every estimate prices
against your real tier:

| Plan | Monthly | Included | Overage |
|---|---:|---:|---:|
| basic | $0 | 1,000 | hard limit — requests just fail |
| pro | $5 | 30,000 | $0.001 |
| ultra | $25 | 300,000 | $0.0009 |
| mega | $250 | 6,000,000 | $0.00005 |

Set `MAPS_QUOTA_RESET_DAY` to the day you subscribed — RapidAPI resets quota
on the subscription anniversary, not the 1st, and the "quota left" figure is
wrong by however many days those differ.

With the default 29,673-ZIP list:

| Job | Requests | Cheapest plan | Cost that month |
|---|---:|---|---:|
| HVAC, one state | 14,126 | pro | **$5** (inside quota) |
| HVAC, OH + MI | 24,908 | pro | **$5** (inside quota) |
| funeral, national | 356,076 | ultra | **$75** ($25 + $50 overage) |
| home services, national | 593,460 | ultra | **$289** ($25 + $264) |
| 10 national verticals | ~5M | mega | **$250** (inside quota) |

`estimate` and `plan` also track how much quota you've already burned this
month (from the `jobs` table) so the number reflects what the run will
*actually* add, not a fresh-quota fiction.

Two corrections to the numbers in the post that prompted this project:

* **"3 million requests for $100" is not an available plan.** The closest is
  mega at $250 for 6M. Every earlier cost in this README was computed from
  that claimed rate and was roughly 4x too low; the table above is from the
  live plan page.
* **"$19 a category"** doesn't match either. One category nationally is
  29,673 requests — inside pro's quota, so effectively $5.

Also watch the **bandwidth platform fee**: 10,240 MB/month included, then
$0.001/MB. A national vertical returning ~30 KB per response lands near that
limit; several verticals a month will exceed it. Rough order: ~$30 extra per
million requests. Check your actual usage on the RapidAPI dashboard rather
than trusting that estimate.

Steps 4–6 are free — html2text is open source and Gemma runs on your own
machine. The owner web-search fallback in step 6 is the only other paid piece, at
$0.0005 per lookup, and only fires for businesses where the website came up
empty.

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

## Emails — read this before you plan a campaign

**Google Maps does not return email addresses.** Not in this API, not in any
of them. Name, phone, website, address, rating — yes. Email, never. Any tool
claiming Maps emails is getting them somewhere else.

So this pipeline gets them somewhere else too, for free: the `enrich` stage is
already downloading each business's homepage, about, team and contact pages
for the classifier, and the contact page is exactly where a local business
puts its address. Emails are pulled from the raw HTML *before* html2text runs,
because the converter discards `mailto:` links — which is where most of them
live. Obfuscated ones (`info [at] example [dot] com`) are decoded too.

Addresses are then ranked, not just collected:

| Beats | Because |
|---|---|
| `margaret@shop.com` over `info@shop.com` | a named human, especially when it matches the owner name found in step 6 |
| `info@shop.com` over `owner@gmail.com` | on the company's own domain |
| anything over `careers@`, `billing@` | not who you want |
| everything over `noreply@`, `webmaster@` | dropped entirely, along with `you@example.com`-style boilerplate |

The CSV gets a chosen `email` plus up to five more in `all_emails`.

**Expect partial coverage.** Roughly half to two-thirds of local businesses on
Maps list a website at all, and not all of those publish an address. So plan
for an email on a meaningful minority of rows, not most of them — and check
your own number after a pilot state rather than trusting mine:

```bash
python -m gmscraper run "HVAC in Ohio, owner name and email" --out out/oh.csv
python -m gmscraper stats     # businesses / with website / domains with email
```

Two things worth knowing:

* **Phone coverage is near-total** and, for funeral homes and HVAC shops,
  a phone number is often the better channel anyway.
* If you need email on the rows the website scrape missed, export the CSV and
  push `owner_name` + `domain` through a dedicated finder. You already have
  LeadMagic, AI Ark and BillionVerify connected on the Claude side — hand me
  the CSV and I can run that waterfall and verify the results.

Always verify before sending. Scraped addresses go stale and hitting dead ones
wrecks your domain reputation.

---

## Export

```bash
python -m gmscraper export --out out/leads.csv                  # in-ICP only
python -m gmscraper export --out out/leads.csv --with-email     # only rows with an email
python -m gmscraper export --out out/leads.csv --with-owner     # named owner only
python -m gmscraper export --out out/oh.csv --states OH PA MI
python -m gmscraper export --out out/good.csv --min-rating 4.0 --min-reviews 20
python -m gmscraper export --out out/all.csv --all              # everything
```

Columns: `place_id, name, owner_name, owner_title, owner_source, email,
all_emails, phone, website, domain, address, city, state, zip, rating,
reviews, main_category, types, latitude, longitude, maps_url, in_icp,
icp_confidence, icp_reason, source_category`.

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
pip install pytest && python -m pytest tests/ -q     # 44 tests, no network, no API key
```

Covers response normalization across differing field names, address parsing,
aggregator rejection, email harvesting/ranking, brief-to-plan parsing,
cross-ZIP dedup, job checkpoint/resume and export filtering.

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
