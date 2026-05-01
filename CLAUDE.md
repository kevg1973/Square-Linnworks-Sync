# Linnworks ↔ Square Sync — Project State

## What this app does

Two independent one-way syncs between Linnworks (source of truth) and
Square (POS terminal at Northwest Guitars):

1. **Linnworks → Square** (cron, every 15 min): push SKU, title,
   price, and stock to Square so the POS reflects current inventory.
2. **Square → Linnworks** (cron, every 5 min): pull POS sales and
   create matching orders in Linnworks. Auto-marked as paid (POS = money
   already taken). NOT auto-dispatched — Kevin manually processes.

A separate **reconciliation** workflow runs on-demand (manually
triggered) and produces a read-only CSV report comparing the two
catalogues by SKU.

Replaces a legacy Square sync app that crashed occasionally and that
Kevin had no way to fix or restart, since someone else built it.

## Project lives at

GitHub repo `linnworks-square-sync` (private). All code, all
workflows, all migrations. No external SSD dependency, unlike the
Easyship app.

## Stack (all locked in)

- **Language**: Python 3.12
- **Compute**: GitHub Actions (cron via `schedule:` triggers + manual
  via `workflow_dispatch`)
- **State**: Supabase — tacked onto an existing project, all tables
  prefixed `sq_`
- **Cost**: £0/month (GitHub free tier, Supabase free, no other
  services)

## Why these choices

### Why GitHub Actions and not Railway / Cloudflare

- GitHub Actions cron is genuinely free for this workload (~190
  minutes/month total across both jobs, well under the 2,000-minute
  free tier).
- Every run is visible in the Actions tab as a clickable log. When
  something fails, Kevin clicks it, sees the error, and re-runs with a
  button. This directly addresses the "I have no control over the old
  app when it breaks" pain point.
- Cloudflare Workers cron has been observed to silently stop firing
  for 24+ hours (community reports March 2026). For a stock sync that
  affects what customers can buy at the till, that's an unacceptable
  failure mode.
- Railway Hobby would work fine but costs £4/month for nothing this
  stack doesn't already have.

### Why Python and not Node

- Linnworks reference doc (sister project) is already written in
  Python idioms. All pattern-matching to that doc is free.
- Square SDK for Python is well-maintained.
- GitHub Actions runs Python natively without setup.
- The Easyship app stays in Node — separate project, no shared code.

### Why Supabase tack-on rather than a new project

- Kevin has used all his free Supabase project slots.
- Paying for Supabase Pro to isolate this sync from another project
  isn't justified ($25/mo for soft isolation).
- All this sync's tables prefixed `sq_*` to avoid collisions with the
  host project's schema.

### Why two crons instead of one daemon

- Independent failure domains. If `stock-push` breaks, orders still
  flow. If `order-pull` breaks, stock still updates.
- No always-on service to babysit, no "is the server up" failure mode.

### Why a few minutes of staleness is acceptable

- Northwest Guitars does ~100 orders/day across Shopify/eBay. Stock
  level lag of 5–20 minutes is within tolerance for a guitar shop —
  POS staff can see what's physically on the shelf, and the count
  reconciles within 20 minutes worst-case.
- The double-sell race (last unit sold on Shopify in the same window
  as a POS sale at the till) is rare and would be handled the same way
  as today (apologise to one customer, refund). System can't prevent
  it without real-time webhooks, and webhooks aren't worth the
  complexity for a once-a-month edge case.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         GITHUB ACTIONS                               │
│                                                                      │
│  ┌────────────────────────┐         ┌────────────────────────────┐  │
│  │ stock-push.yml         │         │ order-pull.yml             │  │
│  │ cron: every 15 min     │         │ cron: every 5 min          │  │
│  │ Linnworks → Square     │         │ Square → Linnworks         │  │
│  └───────────┬────────────┘         └────────────┬───────────────┘  │
│              │                                    │                  │
│  ┌───────────┴────────────────────────────────────┴───────────────┐ │
│  │ reconcile.yml (workflow_dispatch only — manual trigger)        │ │
│  │ Read-only audit: writes CSV report to artifact                 │ │
│  └───────────┬────────────────────────────────────────────────────┘ │
└──────────────┼──────────────────────────────────────────────────────┘
               │
               ▼
       ┌───────────────┐         ┌──────────────┐         ┌────────────┐
       │   LINNWORKS   │◄───────►│   SUPABASE   │◄───────►│   SQUARE   │
       │  (truth)      │         │  (audit log, │         │  (POS)     │
       │               │         │   sku map,   │         │            │
       │               │         │   watermarks)│         │            │
       └───────────────┘         └──────────────┘         └────────────┘
```

## Stock decrement flow (the loop)

```
Square sale fires (POS at till)
    │
    ▼
order-pull cron picks it up (within 5 min)
    │
    ▼
Create Linnworks order (Source = "DIRECT", auto-marked as paid)
    │ (Linnworks auto-decrements stock when the order lands)
    ▼
Order sits "open" in Linnworks for Kevin to process manually
    │
    │ (meanwhile, separately:)
    ▼
stock-push cron runs (every 15 min)
    │
    ▼
Reads current Linnworks stock for all SKUs
    │
    ▼
Pushes deltas to Square via Catalog + Inventory APIs
```

Linnworks is the only system that decrements stock on its own. Square
gets corrected to whatever Linnworks says. No two-way decrement, no
race conditions on the truth.

## Phasing

The build is intentionally phased so each step produces a runnable
deliverable that can be tested before the next is built on top.

| Phase | What | Status |
|---|---|---|
| **0a** | Repo skeleton + auth smoke test | ✅ complete (2026-05-01) |
| 0b | Diagnostic probes for the four unknowns | in progress |
| 1 | Reconciliation report (read-only, both APIs) | not started |
| 2 | Stock-push cron (Linnworks → Square, write to Square) | not started |
| 3 | Order-pull cron (Square → Linnworks, write to Linnworks) | not started |
| 4 | Operational dashboard | not started |
| 5 | Orphan-deletion path on reconciliation (with confirm step) | not started |

## Phase 0a — what's here now

- Repo skeleton with the directory layout we've agreed
- Supabase migration (`supabase/001_initial.sql`) with five `sq_*`
  tables: `sq_sku_map`, `sq_sync_runs`, `sq_square_orders_processed`,
  `sq_errors`, `sq_watermarks`
- `lib/config.py` — env var loading with loud failures
- `lib/linnworks.py` — auth + cluster discovery (reused pattern from
  Easyship app's Linnworks integration)
- `lib/square.py` — auth check + request helper
- `lib/db.py` — Supabase client and audit-log helpers
- `.github/workflows/smoke-test.yml` — manual-trigger workflow that:
  1. Authenticates against Linnworks
  2. Authenticates against Square (calls `/v2/locations` as the
     auth check)
  3. Round-trips Supabase (writes a row to `sq_sync_runs` with
     status=success, then reads it back)
  4. Prints success and exits

If the smoke test passes, all credentials are wired correctly and we
can start Phase 0b.

## Phase 0a — completed (2026-05-01)

The smoke test workflow ran green on 2026-05-01:

- **Linnworks auth** — install token exchanged, session token + cluster
  URL returned. Tenant cluster confirmed as the EU cluster.
- **Square auth** — `/v2/locations` returned a non-zero list. The
  physical-shop location for inventory writes is **`L74KSP08AJ2GH`**
  (Northwest Guitars). This is the location ID we'll pin for all
  inventory operations from Phase 2 onward. (Will be promoted to a
  `SQUARE_LOCATION_ID` env var when stock-push lands; for now the
  probes derive it at runtime from `/v2/locations`.)
- **Supabase round-trip** — wrote a `smoke-test` row into
  `sq_sync_runs`, marked it finished, read it back. Service-role key
  has full access to the `sq_*` tables.

All seven repository secrets are wired correctly. Cleared to start
Phase 0b.

## Phase 0b — known unknowns to probe

These are deliberately deferred from Phase 0a because we can't resolve
them without running code against real APIs:

1. **Square scopes on Kevin's existing app.** Need `ITEMS_READ`,
   `ITEMS_WRITE`, `INVENTORY_READ`, `INVENTORY_WRITE`, `ORDERS_READ`.
   Probe by calling each endpoint and observing whether we get
   `INSUFFICIENT_SCOPES` errors. If anything's missing, add the scope
   and re-issue the access token.

2. **Square Location ID for the physical shop.** Inventory counts in
   Square are per-location. Probe `/v2/locations` and pick the one
   matching "Northwest Guitars" (or whatever it's called). Pin it as
   `SQUARE_LOCATION_ID` in env vars from then on.

3. **Linnworks `Orders/CreateNewOrder` body shape.** Per the
   diagnostic-first pattern in §10 of `LINNWORKS_REFERENCE.md`, this
   is tenant-dependent and the docs are stale. Probe with multiple
   body shapes (flat / request-wrapped / SearchParameters-wrapped)
   until one returns 200. Lock that shape in.

4. **How to mark a Linnworks order as paid without dispatching.**
   Possibly a field on `CreateNewOrder` itself; possibly a separate
   `Orders/SetOrderPaymentStatus` (or similar) call. Probe.

Each probe is a standalone Python script in `probes/`, runnable via
its own `workflow_dispatch` workflow. Output goes to `DISCOVERIES.md`
which is committed back to the repo as a permanent record of what
shape works on Kevin's tenant.

## Required env vars / GitHub Actions secrets

```
LINNWORKS_APP_ID         # From Linnworks Developer Dashboard
LINNWORKS_APP_SECRET     # From Linnworks Developer Dashboard
LINNWORKS_TOKEN          # Install token for Northwest Guitars tenant
                         #   (these three are the same as the Easyship app —
                         #    can be reused verbatim from .env there)

SQUARE_ACCESS_TOKEN      # Production token, starts with EAAA...
                         #   From developer.squareup.com → app → Credentials
                         #   → toggle "Production" → Show Access Token
SQUARE_APPLICATION_ID    # Production Application ID (same page)

SUPABASE_URL             # https://<project-id>.supabase.co
SUPABASE_SERVICE_KEY     # service_role key (NOT anon key) — has full DB access
                         #   Settings → API → service_role secret
```

All seven go into GitHub repo Settings → Secrets and variables →
Actions as repository secrets. None are committed to the repo.

## Square credential gotcha

Square has two kinds of access token:

- **Personal access token** — full account access, no scopes,
  never expires. What you want for a server-side integration like
  this. Marker: starts with `EAAA...`.
- **OAuth access token** — scoped, expires every 30 days, requires a
  refresh-token flow. Used when an app accesses *other people's*
  Square accounts. Marker: also starts with `EAAA...` but the
  developer dashboard distinguishes them.

The old sync app most likely used a personal access token (simpler,
no refresh flow needed). If the smoke test fails with
"AUTHENTICATION_ERROR / token expired", we're dealing with OAuth and
need to add a refresh handler.

## Linnworks gotchas (carried over from sister project)

See `LINNWORKS_REFERENCE.md` for the full list. The ones most relevant
to this project:

- Auth response includes a `Server` URL — use that, don't hardcode the
  cluster.
- Authorization header is the raw session token, no `Bearer` prefix.
- 401 on any call → re-auth once, retry once. After that, fail loud.
- Rate limit ~1 req/sec on most endpoints. Sleep ~1.1s between calls
  during heavy reads (stock-push pulling N items will need this).
- For matching Square orders to Linnworks orders by reference number:
  Linnworks orders created from Square will have `Source = "DIRECT"`
  (we set it on creation). This is different from the Easyship app
  which filters to `Source = "SHOPIFY"` — see the note in that
  project's CLAUDE.md.
- The shape of `Orders/CreateNewOrder` is one of the Phase 0b
  unknowns. Don't assume it works until probed.

## Square API notes

- **Catalog and Inventory are separate APIs.** Updating an item's
  price/title is `Catalog`. Updating its stock level is `Inventory`.
  Two calls per item per push.
- **Catalog upsert is keyed on Square's catalog object ID, not SKU.**
  We must remember the ID Square assigned the first time we created
  each item. That mapping lives in `sq_sku_map`.
- **`BatchUpsertCatalogObjects` lets us send up to ~1000 items in one
  call.** Use that for stock-push, not per-item calls.
- **Inventory counts are per-location.** Phase 0b will pin the right
  location ID.
- **Rate limit: 10 req/sec.** Generous, but batch endpoints are still
  the right way to use them.
- **Square Webhooks API requires the personal access token, not OAuth.**
  Not relevant for v1 (no webhooks) but worth knowing if we ever add
  real-time triggers.

## State management (Supabase tables)

All prefixed `sq_` to avoid collision with the host project.

| Table | Purpose |
|---|---|
| `sq_sku_map` | The spine. One row per SKU, links Linnworks `StockItemId` to Square `catalog_object_id` + `variation_id`. Also holds `last_known_stock` and `last_known_price` so we can skip no-op writes. |
| `sq_sync_runs` | One row per cron execution. Records status, item counts, GitHub run URL for click-through to logs. |
| `sq_square_orders_processed` | Audit log + idempotency for order-pull. Keyed on Square's `order_id`. Prevents double-creation of Linnworks orders. |
| `sq_errors` | Per-error log with context JSON. `job_name`, `occurred_at`, `message`, `context`. |
| `sq_watermarks` | Key-value store for `last_pulled_at`-style cursors. Currently has `square_orders_last_pulled_at`. |

See `supabase/001_initial.sql` for the actual schema.

## What was tried and rejected

(Empty so far — populate as we encounter dead-ends.)

## Diagnostic-first development pattern

Per §10 of `LINNWORKS_REFERENCE.md`. Every endpoint shape we don't
already know gets probed by a standalone script in `probes/` before
production code is written against it. The probe script is preserved
in the repo permanently — it's how we'll re-validate when an API
silently changes.

## Where to start when resuming

1. Is the smoke test still passing? Run it manually from the Actions
   tab. If yes, all credentials are good.
2. What phase are we in? Check the table above and the most recent
   workflow files committed.
3. What's been discovered? Read `DISCOVERIES.md` (created in Phase 0b)
   for endpoint shapes and tenant-specific facts.

## Kevin's preferred working style

(Carried over from sister project's CLAUDE.md.)

- No coding background — uses Claude Code for all implementation via
  detailed prompts.
- GitHub Desktop for version control (not CLI git).
- Prefers numbered update logs and step-by-step instructions.
- Prefers visual UI feedback over silent terminal output.
- Wants reliable solutions over fancy ones — was burned multiple
  times by macOS permission battles during the Easyship build.

## Likely next features (deferred from Phase 0a)

- Phase 0b: the four diagnostic probes
- Phase 1: reconciliation report
- Phase 2 onwards as listed in the phasing table above
- Slack notifications when a sync run fails (re-uses Slack token from
  Easyship project — same workspace, same bot user)
