# Working in this repo

A lead-generation pipeline: scrape US local businesses off Google Maps, then
qualify them with a local LLM. Josh drives this conversationally — he will say
things like *"get me HVAC companies in Ohio with owner names and emails"* and
expects a CSV at the end, not a lecture on flags.

Run everything through the CLI below. Don't write ad-hoc scrapers or one-off
scripts; the pipeline already handles retries, checkpointing and dedup.

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
3. Show **one line**: `24,908 requests, $0.83, 1,916 zips, 13 categories`.
4. Under `AUTO_APPROVE_UNDER`? Just go. Over it? Ask once, then wait.
5. Run every stage. Report rows, email coverage %, owner coverage %, and ~15
   sample rows.

Setup steps (venv, `.env`, `ollama list`, `zips`) are one-time. Do them
silently if they're missing; don't make him watch.

## Spending rules

`scrape` and `run` cost real money on Josh's RapidAPI plan. Everything else
is free.

```
AUTO_APPROVE_UNDER = $2.00
```

1. **Under $2, just run it** — show the number, don't wait. That covers any
   single state, which is most requests. **Over $2, ask once** with the
   dollar figure, and wait for a yes. Never pass `--yes` to `run` for an
   over-threshold job he hasn't approved in the conversation.
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
python -m gmscraper classify --vertical hvac --workers 2
python -m gmscraper owners --workers 2 [--fallback]    # --fallback is paid
python -m gmscraper export --out out/x.csv --with-email --min-rating 4.0
python -m gmscraper stats
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
| "it stopped / I killed it" | just re-run the same command. Every stage resumes from SQLite |
| "how much have I spent" | `stats` → `jobs done` × `PRICE_PER_REQUEST` |

## Things that will bite you

- **Ollama must be running** (`ollama serve`) with `gemma4:12b` pulled, for
  `plan`, `classify` and `owners`. Check with `ollama list` before blaming code.
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
  classify.py    ICP verdict via local Gemma
  owner.py       owner-name extraction (+ OpenWeb Ninja fallback)
  store.py       SQLite: jobs, businesses, sites, emails, verdicts, owners
  export.py      CSV
config/categories.yml   verticals: icp + category aliases
tests/                  29 offline tests, no network/API key needed
```

Run `python -m pytest tests/ -q` after changing normalization, ranking,
export filters or the store schema.
