# Working in this repo

A lead-generation pipeline: scrape US local businesses off Google Maps, then
qualify them with a local LLM. Josh drives this conversationally — he will say
things like *"get me HVAC companies in Ohio with owner names and emails"* and
expects a CSV at the end, not a lecture on flags.

Run everything through the CLI below. Don't write ad-hoc scrapers or one-off
scripts; the pipeline already handles retries, checkpointing and dedup.

## Claude MCP server

Prefer the MCP server when Josh is in Claude Desktop / Claude Code / Cursor:

```bash
python -m mcp_server
```

Config examples: `mcp_server/README.md`, `mcp_server/claude_desktop.example.json`, `.cursor/mcp.json`.

The MCP server ships a playbook Claude reads automatically (`instructions` +
`gmscraper://playbook` + prompts `find_leads` / `when_to_use`):

1. Use this MCP for US local-business lead lists (niche + state/city)
2. `plan_leads` → show cost → `run_leads` (no spend approval)
3. Ask before nationwide; no connector login/OAuth

## The default interaction

Josh opens a terminal and types one sentence:

> *"find me funeral homes in Texas with 4+ stars, I need owner names and emails"*

That is a complete instruction. Take it and run the whole thing to a CSV.
Don't hand back a checklist, don't ask which flags he wants, don't walk him
through the stages. He wants the file.

The flow, every time:

1. `plan` the brief.
2. Sanity-check the category list yourself before pricing it. If an obvious
   Maps synonym is missing, add it — a missing category is a missing slice of
   the market, and this is the single biggest driver of list quality.
3. Show **one line**: `24,908 requests, $0 (inside ultra quota), 1,916 zips,
   13 categories`.
4. Show the cost, then run immediately — do not ask for spend approval.
5. Run every stage. Report rows, email coverage %, owner coverage %, and ~15
   sample rows.

Setup steps (venv, `.env`, `ollama list`, `zips`) are one-time. Do them
silently if they're missing; don't make him watch.

## Spending rules

`scrape` and `run` cost real money on Josh's RapidAPI plan. Everything else
is free.

Billing is a monthly plan + quota, not cents per request — `estimate` and
`plan` already price against `MAPS_PLAN` and subtract quota already used this
month. Read the `est. cost` line they print, not a per-request rate.

1. **Always run after showing the estimate** — show the number, don't wait for
   a yes. There is no spend-approval gate. Josh is on `ultra`: 300,000
   requests a month included. If the estimate says BLOCKED, the plan dropped
   to `basic` — tell him, don't try to run it.
2. **No region named?** Ask which state(s) before running. Nationwide is
   20–30x the cost of one state — never assume it.
3. **Run `probe` before the first scrape in a fresh checkout or after any
   `.env` change.** It makes one request and prints the raw API response. If
   fields come back empty, fix `ALIASES` in `gmscraper/mapsdata.py`, then run
   `renormalize` — never re-scrape to fix a mapping problem.
4. **Pilot one state before going national.** For a first-time vertical, run
   `--states <one>` and show him the sample before offering the national run.
   Once he's seen the quality, a national run is a normal over-threshold ask.
5. Free stages (`enrich`, `classify`, `owners` without `--fallback`, `export`,
   `stats`, `plan`, `zips`, `estimate`) never need asking. Re-run them freely.

## The commands

```bash
python -m gmscraper zips                      # once per checkout, offline, ~2s
python -m gmscraper plan "<brief>"            # free: brief -> categories + cost
python -m gmscraper plan "<brief>" --save plans/x.json
python -m gmscraper probe --zip 44301 --category "hvac contractor"
python -m gmscraper run "<brief>" --out out/x.csv      # all stages, asks first
python -m gmscraper estimate --vertical hvac --states OH
python -m gmscraper scrape --vertical hvac --states OH --workers 8
python -m gmscraper enrich --workers 12
python -m gmscraper classify --vertical hvac --workers 1
python -m gmscraper owners --workers 1 [--fallback]    # --fallback is paid, ~$0.0005/lookup
python -m gmscraper export --out out/x.csv --with-email --min-rating 4.0
python -m gmscraper stats
python -m gmscraper bench                     # measure LLM speed, pick a model
python -m gmscraper renormalize               # re-map stored raw JSON, 0 API calls
```

`run` chains scrape → enrich → classify → owners → export. Use the individual
stages when re-running just one part (e.g. re-classify against a tighter ICP
without re-scraping).

## Common requests → what to do

| Josh says | Do |
|---|---|
| "get me X in Y" | the default flow above — plan, one cost line, run, report |
| "/leads <brief>" | same thing; the slash command just wraps it |
| "that list is too broad / has junk in it" | tighten the `icp:` exclusions, re-run `classify` only. Do **not** re-scrape |
| "I need more of them" | add category aliases (the usual cause of a short list), `estimate`, then scrape the new categories — existing ones are already checkpointed and won't re-charge |
| "no emails in the CSV" | check `stats` for `domains with email`. Maps returns no emails; they come from the website scrape, so coverage is partial by nature. Say so plainly rather than implying it's a bug |
| "classify/owners is too slow" | on cloud, raise `--workers`; it is network-bound. On local, `bench` first. Then lower `LLM_MAX_EVIDENCE_CHARS` before reaching for a smaller model — a 12k-char prompt is ~3k tokens of prefill and most of it is nav and footer boilerplate |
| "it stopped / I killed it" | just re-run the same command. Every stage resumes from SQLite |
| "how much have I spent" | `estimate` prints quota used this billing cycle and the overage on top |
| "quota numbers look wrong" | `MAPS_QUOTA_RESET_DAY` in `.env` must be the day he subscribed — RapidAPI resets on the anniversary, not the 1st |

## Things that will bite you

- **The owner fallback is ScraperLink on Apify, not OpenWeb Ninja.** We
  switched: OpenWeb Ninja charged ~$0.0025 per search *plus $25/month*, and
  this stage only fires on leftovers (in-ICP businesses whose own site did not
  name an owner), so a standing monthly fee was the wrong shape. ScraperLink
  is $0.0005 per search with no floor, and Apify's free tier ($5/mo credit)
  covers ~10,000 lookups. Needs `APIFY_TOKEN` in `.env`; blank means the
  stage skips itself and `owners` runs website-only. `--fallback-source
  openwebninja` still works for anyone already subscribed.
  Neither backend returns Google's AI Overview — that is a $0.003 add-on on
  Apify's *official* actor (15x the price). Don't add it without measuring:
  the overview is synthesised from the same organic snippets we already read,
  and it strips the attribution the extractor uses to avoid guessing.
- **Watch the `schema repairs` count in the spend line.** Zero is expected.
  Nonzero means the endpoint is not enforcing the JSON schema and responses
  are being corrected on a retry — common on OpenRouter, where a model is
  served by several providers and only some support `json_schema`. It still
  produces correct rows, but it costs extra calls; switch provider or route
  if it climbs.
- **The LLM stages run on `gpt-5-nano` by default, not locally.** A national
  vertical costs about $2 and a state about $0.09 — 2-4% of the Maps scrape,
  so it is not worth optimising. `LLM_PROVIDER=ollama` switches back to local
  (free, offline, but slow on Josh's CPU-only box). Every stage prints its
  own token spend. Workers default to 8 for cloud and 1 for local.
- **Only if `LLM_PROVIDER=ollama`:** Ollama must be running (`ollama serve`)
  with the model in `OLLAMA_MODEL` pulled. Check `ollama list` before blaming
  code.
- **Josh's machine is CPU-only** (Snapdragon X Plus, 16 GB, no GPU — Ollama
  does not use the NPU). This only matters on `LLM_PROVIDER=ollama`; the
  default cloud path sidesteps it. A 12B is minutes per business there; the
  local default is `gemma4:e4b`. If a local stage is slow or timing out:
  (1) lower `LLM_MAX_EVIDENCE_CHARS` — prefill dominates on CPU and it is the
  biggest lever, (2) a smaller model (`gemma4:e2b`, `qwen3.5:4b`,
  `phi4-mini`), (3) `--workers 1`, which is already the default. More workers
  make it *worse* on CPU: threads fight for the same cores. Run
  `python -m gmscraper bench` to measure rather than guess.
- **If he says the machine is unusable while a stage runs**, that is a
  different problem from "slow": set `OLLAMA_NUM_THREADS` (or `--threads`) to
  4-6 so inference leaves cores for his browser. Costs throughput on a job
  that runs unattended anyway. For RAM pressure, `OLLAMA_KEEP_ALIVE=0`
  releases the weights when a stage ends, and `gemma4:e2b` is ~1.6 GB
  smaller than `e4b`. Note an NPU runtime would NOT help RAM — Snapdragon
  uses unified memory, so the weights sit in system RAM either way.
- **`.env` is gitignored and holds live API keys.** Never commit it, never
  paste key values into commit messages, PR bodies or comments.
- **`leads.db` is the state.** Deleting it discards paid work — all scraped
  businesses. Never delete it to "start clean"; use `--states` / a second
  `--db` path for a separate list.
- **Never lower the ICP bar to make a list look bigger.** A padded list is
  worse than a short one for cold outreach.
- The owner/email extractors are told to return null rather than guess. Keep
  that. A wrong first name in a cold email is worse than no first name.

## Layout

```
gmscraper/
  cli.py         subcommands (start here)
  brief.py       natural language -> Plan
  mapsdata.py    RapidAPI client + tolerant field normalizer (ALIASES)
  scrape.py      (zip x category) fan-out, checkpointed
  enrich_site.py html2text website fetch
  emails.py      email harvest + ranking
  evidence.py    trim page text to what answers the question (CPU prefill)
  bench.py       measure prefill/generation speed, project a run
  classify.py    ICP verdict via local Gemma
  owner.py       owner-name extraction (+ web-search fallback)
  websearch.py   SERP backends: ScraperLink/Apify (default), OpenWeb Ninja
  store.py       SQLite: jobs, businesses, sites, emails, verdicts, owners
  export.py      CSV
config/categories.yml   verticals: icp + category aliases
tests/                  55 offline tests, no network/API key needed
```

Run `python -m pytest tests/ -q` after changing normalization, ranking,
export filters or the store schema.
