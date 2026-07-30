---
description: Build a lead list from a plain-English brief
argument-hint: funeral homes in Texas, 4+ stars, owner names and emails
---

Build a lead list for this brief:

$ARGUMENTS

Follow the steady-state flow in CLAUDE.md. In short:

1. `plan` the brief. Read the category list back to me and sanity-check it
   yourself — if an obvious Maps synonym for this business type is missing,
   add it before pricing, because a missing category is a missing slice of
   the market.
2. Show me one line: `<n> requests, $<cost>, <n> zips, <n> categories`.
   Auto-approve and continue if it is under the AUTO_APPROVE_UNDER threshold
   in CLAUDE.md. Otherwise ask me once, and wait.
3. Run all stages to a CSV in `out/`, named after the vertical and region.
4. Report: total rows, share with an email, share with an owner name, and
   15 sample rows. Be straight about the coverage numbers — partial email
   coverage is expected, not a bug to explain away.

If I gave no region, ask me before running: nationwide is 20-30x the cost of
one state and that is not a decision to make on my behalf.
