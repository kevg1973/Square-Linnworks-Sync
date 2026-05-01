# linnworks-square-sync

Two-way sync between Linnworks (source of truth for stock and orders)
and Square (POS terminal at Northwest Guitars). Replaces the legacy
Square sync app.

## Status

**Phase 0a — smoke test only.** This repo authenticates against
Linnworks, Square, and Supabase, and exits successfully. No production
data is read or written yet.

See [CLAUDE.md](./CLAUDE.md) for the full project state, architectural
decisions, and where we are in the build.

## What this will eventually do

- **Linnworks → Square**: every 15 min, push product info (SKU, title,
  price, stock level) from Linnworks to Square so the POS terminal
  reflects current inventory.
- **Square → Linnworks**: every 5 min, pull new Square POS sales and
  create matching orders in Linnworks (auto-marked as paid, left open
  for manual processing).
- **Reconciliation**: on-demand, read-only audit comparing the two
  catalogues, written to a CSV artifact.

## Quick start

1. Clone this repo.
2. In Supabase, run the migration in [supabase/001_initial.sql](./supabase/001_initial.sql)
   against your existing project. All tables are prefixed `sq_`.
3. In GitHub repo Settings → Secrets and variables → Actions, add the
   secrets listed in [`.env.example`](./.env.example) (also see CLAUDE.md
   for which value goes where).
4. Run the **Smoke Test** workflow manually from the Actions tab. If it
   passes, Phase 0a is done.

## Repo layout

```
.github/workflows/
  smoke-test.yml           Phase 0a — auths against everything and exits
                           (more workflows added in later phases)

lib/
  __init__.py
  config.py                Loads env vars, fails loud if anything is missing
  linnworks.py             Auth + cluster discovery, request helper
  square.py                Auth check + request helper
  db.py                    Supabase client, audit-log helpers

probes/
  README.md                What probes exist and how to run them
                           (populated in Phase 0b)

supabase/
  001_initial.sql          Five tables, all prefixed sq_

CLAUDE.md                  Project state — read this first when resuming
LINNWORKS_REFERENCE.md     Working reference for Linnworks API
                           (copied from sister project)
.env.example               Required environment variables
.gitignore
README.md                  You are here
```

## Phasing

| Phase | What | Status |
|---|---|---|
| 0a | Repo skeleton + auth smoke test | **← this commit** |
| 0b | Diagnostic probes for the unknowns | not started |
| 1 | Reconciliation report (read-only) | not started |
| 2 | Stock-push cron (Linnworks → Square) | not started |
| 3 | Order-pull cron (Square → Linnworks) | not started |
| 4 | Operational dashboard | not started |
| 5 | Orphan deletion path on reconciliation | not started |
