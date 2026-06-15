# Linnworks ↔ Square Sync — Project State

## Current State (May 6, 2026)

**Project location**: `/Volumes/Music/Github/Square Linnworks Sync` (local),
`kevg1973/Square-Linnworks-Sync` (private GitHub repo).

**Goal**: Replace the legacy Square sync with a clean rebuild.
Linnworks is the source of truth (~100 orders/day across Shopify and
eBay). Square is used for POS + Appointments only. **Cutover complete
— sync is live in production.**

**Stack**: Python 3.12, Railway cron (scheduled jobs, Dockerfile-built),
GitHub Actions (manual triggers only — observe-mode debugging, probes,
wipe), Supabase (project `cart-upsell-tracker`, all tables prefixed
`sq_*`).

**Phase status**:

| Phase | What | Status |
|---|---|---|
| **0a** | Repo skeleton + auth smoke test                              | ✅ complete (2026-05-01) |
| **0b** | Diagnostic probes (Square scopes, Linnworks endpoints)       | ✅ complete (2026-05-02) — 4/4 probes green, mark-paid recipe locked in |
| **1**  | Wipe + rebuild Square's retail catalog from Linnworks        | ✅ complete — catalog rebuilt, ~4,193 retail items live |
| **2**  | Sync cron (Linnworks → Square, every 30 min on Railway)      | ✅ complete — running on Railway as the `sync` service |
| **3**  | Order-pull cron (Square → Linnworks, every 5 min on Railway) | ✅ complete — verified end-to-end with a real Square test order |
| 4      | Operational dashboard                                         | not started |

## What's been built and tested

### Wipe tool — ✅ COMPLETE

`tools/wipe_square_items.py` + `.github/workflows/wipe-square-items.yml`.

- Walks the entire Square catalog, classifies each item by
  `item_data.product_type`. Anything with an unknown/missing
  `product_type` is skipped and reported (never deleted).
- `--write` flag required to actually delete; default is observe.
  Workflow `mode` input dropdown also defaults to `observe`.
- `--limit N` for staged write runs.
- Audit log to `sq_wipe_log` (one row per run, observe or write).
- Cutover wipe deleted ≈7,381 retail items, kept 9 services.
- Manual-only — no schedule. Triggered via GitHub Actions
  `workflow_dispatch`.

### Sync tool — ✅ LIVE (Railway, every 30 min)

`tools/sync_linnworks_to_square.py`, deployed via `railway.sync.json`
(builder: Dockerfile). GitHub Actions workflow
`.github/workflows/sync-linnworks-to-square.yml` retained for manual
observe-mode runs (`schedule:` block commented out).

- Pulls all Linnworks SKUs via `Stock/GetStockItemsFull` (paged 200
  at a time; partial-page detection for end-of-catalog), walks
  Square's REGULAR catalog, fetches current Square inventory,
  classifies each Linnworks item as **CREATE / UPDATE / STOCK_ONLY /
  NO_OP**.
- CREATE/UPDATE go through `catalog/batch-upsert` (chunks of 100 with
  per-batch `idempotency_key`); UPDATEs include item-level + variation-
  level `version` for optimistic concurrency. Stock pushes go through
  `inventory/changes/batch-create` as `PHYSICAL_COUNT` against
  `L74KSP08AJ2GH`.
- `--write` flag, `--limit N`, audit log to `sq_lw_sync_log`.
- Railway cron always invokes with `--write`. GitHub `workflow_dispatch`
  defaults to observe, kept for ad-hoc dry runs and staged rollouts.

### Order-pull tool — ✅ LIVE (Railway, every 5 min)

`tools/pull_square_orders_to_linnworks.py`, deployed via
`railway.pull.json` (builder: Dockerfile). GitHub Actions workflow
`.github/workflows/pull-square-orders-to-linnworks.yml` retained for
manual observe-mode runs (`schedule:` block commented out).

- Pulls Square orders since `sq_watermarks.square_orders_last_pulled_at`,
  creates Linnworks orders idempotently keyed on
  `(Source, SubSource, ReferenceNumber)` where `ReferenceNumber` is
  derived deterministically from Square's `order_id`.
- Two-step mark-paid: `Orders/ChangeOrderTag` (unpark) →
  `Orders/ChangeStatus` (status=1). Both
  `application/x-www-form-urlencoded`. (See *Discoveries* for the
  recipe — skipping the unpark makes step 2 silently no-op.)
- Line items linked to existing Linnworks stock items by SKU
  (`AutomaticallyLinkBySKU`), with the resolved Linnworks
  `StockItemId` attached on the line so Linnworks decrements stock on
  payment.
- VAT is back-calculated from Linnworks-stored VAT-inclusive prices.
- Service-SKU line items flagged with `isService` so Linnworks
  doesn't try to decrement a stock item.
- Watermark advances on success; orders already in
  `sq_square_orders_processed` are skipped without reprocessing.
- Audit log to `sq_orders_pull_log` (per-run summary).

End-to-end verified: real Square test order placed at the till →
landed in Linnworks within 5 min via Railway cron, with linked stock
item, correct VAT extraction, paid status, idempotency on re-run, and
watermark advance.

## Discoveries to remember

These are the load-bearing facts learned from running probes and
observe-mode tools. `DISCOVERIES.md` has the full evidence; this is
the production-relevant summary.

### Catalog discrimination

- **Square catalog discrimination = `item_data.product_type`.**
  `REGULAR` is retail; `APPOINTMENTS_SERVICE` is the 9 services in
  Square's Service library. This is structural, not heuristic — don't
  try to infer service-ness from `available_for_booking` or
  `service_duration`, both are unreliable.
- **Linnworks `IsNotTrackable` comes back as `None`** on this
  tenant — unreliable for filtering services.
- **Square has 1,023 duplicate SKUs.** Some are exact duplicate
  items, some have *different names on the same SKU* (real data-
  integrity bugs from the legacy sync). The wipe nukes them all; the
  sync's duplicate handler arbitrarily picks the first match and
  warns.
- **Square data is non-precious.** All Square data is downstream from
  Linnworks. No sales-history attribution, no per-user assignments.
  Wipe-and-rebuild is safe.
- **Service SKUs in Linnworks (`GTR-001` through `GTR-010`) have fake
  stock counts** (e.g. 4,829). Per Kevin's decision: don't filter,
  push everything; services land in Square's Item library as harmless
  clutter. They won't affect Square Appointments, which is a separate
  library.

### Square API conventions

- **Pricing**: Linnworks `RetailPrice` (decimal pounds) × 100 = Square
  `price_money.amount` (pence).
- **Stock model**: absolute values via `inventory/changes/batch-create`
  with `type: PHYSICAL_COUNT`. Not adjustments — push the desired
  level directly.
- **Catalog upsert is keyed on Square's catalog object id, not SKU.**
  The mapping has to be remembered (we re-derive it from the catalog
  walk on each sync run for now; will be cached in `sq_sku_map` once
  that table is wired up).
- **Square location ID**: `L74KSP08AJ2GH` (Northwest Guitars). Pinned
  in code — should be lifted to a `SQUARE_LOCATION_ID` env var when
  convenient.
- **Catalog and Inventory are separate APIs.** Two write calls per
  item per push (one upsert, one stock change).
- **Rate limit**: 10 req/sec. Generous, but use batch endpoints.
  Sync sleeps 0.2s between batches (~5 req/sec — well under).

### Linnworks API conventions

- **Cluster URL** returned by `Auth/AuthorizeByApplication`. This
  tenant is `https://eu-ext.linnworks.net`. **Never hardcode** — use
  the URL the auth response gives you.
- **Auth header is the raw session token, no `Bearer` prefix.**
- **401 on any call → re-auth once, retry once.** After that, fail
  loud. `lib/linnworks.py` handles this.
- **Rate limit ~1 req/sec.** Sleep ~1.1s between calls during heavy
  reads.
- **Default stock location** = `00000000-0000-0000-0000-000000000000`
  (zero UUID).
- **`Stock/GetStockItemsFull` pagination** terminates by *partial
  page* (page returns < `entriesPerPage` items) **or** by HTTP 400
  when you walk off the end. Sync handles partial-page detection
  cleanly; if a 400 fires after a known partial page, it's treated as
  end-of-catalog. Anywhere else, a 400 propagates as a real error.

### `Orders/CreateOrders`

- `POST /api/Orders/CreateOrders` — JSON body `{"orders": [<order>]}`.
  Plural; even for a single order. Response is a bare JSON array of
  `pkOrderID` strings (not wrapped in `{Orders: [...]}`).
- Required fields: `Source`, `SubSource`, `ReferenceNumber`,
  `ExternalReferenceNumber`, `ReceivedDate`, `DispatchBy`,
  `LocationId`, `Currency`, `OrderItems`, `DeliveryAddress`,
  `BillingAddress`. The address must be named `DeliveryAddress`,
  NOT `ShippingAddress`.
- **Dedup key** is `(Source, SubSource, ReferenceNumber)`. Order-pull
  derives `ReferenceNumber` deterministically from Square's order id
  so retries are naturally idempotent.
- Direct-source orders land **parked** (`IsParked: true`).

### Mark-as-paid recipe (locked in 2026-05-02 by UI capture)

Two-step, both `application/x-www-form-urlencoded`:

1. **Unpark** — `POST /api/Orders/ChangeOrderTag`,
   form body `orderIds=["<uuid>"]` (the value is a JSON-encoded array
   as a string, then URL-encoded; same trick as
   `Dashboards/ExecuteCustomPagedScript`'s `parameters`). No other
   fields. In Python: `form = {"orderIds": json.dumps([pk])}`.
2. **Mark paid** — `POST /api/Orders/ChangeStatus`,
   form body `orderIds=["<uuid>"]&status=1`. Status enum: `0` = Unpaid,
   `1` = Paid.

**Critical**: step 1 MUST run before step 2. Skipping the unpark
makes step 2 silently no-op (returns 200, doesn't change `Status`).
Linnworks server-side stamps `PaidDateTime` automatically when
`Status` flips to `1`.

## Required environment variables (all 7 set on both platforms)

```
LINNWORKS_APP_ID
LINNWORKS_APP_SECRET
LINNWORKS_TOKEN
SQUARE_ACCESS_TOKEN
SQUARE_APPLICATION_ID
SUPABASE_URL              (https://miicdzowfzxffnorlqzp.supabase.co)
SUPABASE_SERVICE_KEY
```

- **GitHub Actions**: repository secrets in Settings → Secrets and
  variables → Actions. Used by manual `workflow_dispatch` runs (sync,
  pull, probes, wipe).
- **Railway**: per-service environment variables in each service's
  Settings → Variables. Used by the live cron services. Both `sync`
  and `pull` services need their own copy.
- None are committed to the repo.

When rotating a secret, rotate it in **both** places. The two stacks
share no env state.

## Repo structure

```
Square-Linnworks-Sync/
├── .github/workflows/
│   ├── smoke-test.yml
│   ├── probe-square-scopes.yml
│   ├── probe-square-catalog.yml
│   ├── probe-square-duplicates.yml
│   ├── probe-square-orders-recent.yml
│   ├── probe-square-services.yml
│   ├── probe-linnworks-create-orders.yml
│   ├── probe-linnworks-mark-paid.yml
│   ├── probe-supabase-write-pattern.yml
│   ├── wipe-square-items.yml                  ✅ manual only
│   ├── sync-linnworks-to-square.yml           ✅ manual only (Railway runs the cron)
│   └── pull-square-orders-to-linnworks.yml    ✅ manual only (Railway runs the cron)
├── lib/                  config.py, linnworks.py, square.py, db.py
├── probes/               probe_*.py (Phase 0b artefacts)
├── tools/
│   ├── __init__.py
│   ├── wipe_square_items.py                   ✅
│   ├── sync_linnworks_to_square.py            ✅ (Railway cron, every 30 min)
│   └── pull_square_orders_to_linnworks.py     ✅ (Railway cron, every 5 min)
├── supabase/001_initial.sql                   sq_* tables (incl. sq_wipe_log, sq_lw_sync_log, sq_orders_pull_log)
├── Dockerfile                                 shared image used by both Railway services
├── railway.sync.json                          sync service config (cron */30, --write)
├── railway.pull.json                          pull service config (cron */5,  --write)
├── RAILWAY.md                                 Railway setup notes
├── CLAUDE.md
├── LINNWORKS_REFERENCE.md
├── DISCOVERIES.md
└── requirements.txt
```

## Cutover (complete)

The Tuesday-morning cutover landed and the production stack is live.
Sequence (chronology in git log):

1. ✅ Migration applied + `NOTIFY pgrst, 'reload schema';`.
2. ✅ Wipe in `--write` mode: ≈7,381 retail items deleted, 9 services kept.
3. ✅ Sync in `--write` mode: ≈4,193 retail items created, stock pushed.
4. ✅ Legacy sync app killed.
5. ✅ Phase 2 sync cron live (initially on GHA, then moved to Railway).
6. ✅ Phase 3 order-pull cron live (initially on GHA, then moved to Railway).
7. ✅ End-to-end verification with a real Square test order: linked
   stock item, correct VAT extraction, paid status, idempotent on
   re-run, watermark advanced.

## Open issues / backlog

**Active issues**:

- **`sq_orders_pull_log` `orders_skipped_empty` PGRST204 — RESOLVED**
  (pending manual apply of `002_*.sql` to the live DB). Root cause was
  **schema drift, not a stale cache**: `001_initial.sql` was edited to
  add the `orders_skipped_empty` column *after* it had already been
  applied to the live database, so the column landed in the source file
  but never on the live table. PostgREST was correctly reporting that
  the column doesn't exist (PGRST204) — no amount of `NOTIFY`-based
  cache reloads would have helped. Fix: `supabase/002_add_orders_skipped_empty.sql`
  (idempotent `ADD COLUMN IF NOT EXISTS` + `NOTIFY`). **Remaining step**:
  Kevin pastes `002_*.sql` into the Supabase SQL editor and runs it;
  then close this item. See *Migration discipline* below for the lesson.
- **Categories on Square items** — POS usability. Sync currently
  doesn't push categories; staff see a flat item list at the till.
- **Service order end-to-end test** — the `isService` flag on
  service-SKU line items is set, but the full flow hasn't been
  exercised by a real booking yet. Will be validated by the next
  service order through the till.
- **429 rate-limit retry/backoff in sync tool** — currently sleeps
  0.2s between batches; if Square returns a 429 the sync just fails.
  Add exponential backoff with retry on 429.
- **`sq_lw_sync_log` retention policy** — table grows unbounded; one
  row per sync run × every 30 min ≈ 48 rows/day ≈ 17k/year. Add a
  periodic prune (keep last 30 days?) or move to a Supabase cron.

**Deferred (out of scope for now)**:

- **Image sync.** Basic SKU/name/price/stock first; product
  pictures are a follow-up.
- **Per-supplier code cache, sales velocity ingest from Script 47**,
  etc.
- **Promote `SQUARE_LOCATION_ID` to an env var.** Currently
  hardcoded as `L74KSP08AJ2GH` across the tools.
- **`sq_sku_map` is unused.** Schema exists but nothing populates it
  yet; sync re-derives the SKU↔catalog-id mapping on every run by
  walking Square's catalog. Caching it would let the sync skip no-op
  writes between runs.
- **Phase 4 — operational dashboard.** Reads `sq_sync_runs`,
  `sq_errors`, `sq_wipe_log`, `sq_lw_sync_log`, `sq_orders_pull_log`,
  surfaces last-run status and recent errors. Shape TBD.
- **Phase 5 — orphan-deletion path on reconciliation** with a
  confirm step.

## Operational gotchas

- **Repo lives on external SSD** `/Volumes/Music/`. Must be mounted
  to commit (Railway and GitHub Actions both run from the cloud, so
  scheduled crons are unaffected by local mount state).
- **macOS leaks `._*` AppleDouble files** when copying onto FAT/exFAT
  volumes. `.gitignore` should swallow them; if any sneak in, delete
  with `find . -name '._*' -delete`.
- **Supabase free Nano projects pause after 7 days inactivity.** Now
  that the sync runs every 30 min the project shouldn't go idle on
  its own — but if cron runs start failing with connection errors,
  check the Supabase dashboard and unpause if needed.
- **Linnworks**: rate limit ~1 req/sec, auth header is the raw token
  (no `Bearer` prefix), 401 → re-auth + retry once.
- **Applying a migration**: paste the SQL into the Supabase SQL editor
  and run it (idempotent — every statement uses `IF NOT EXISTS`),
  **then** run `NOTIFY pgrst, 'reload schema';` so PostgREST sees the
  change. Without the NOTIFY, audit inserts will fail with `PGRST205`
  for ~minutes until the cache naturally refreshes. **New schema
  changes go in a new numbered file, not by editing an already-applied
  one** — see *Migration discipline*.
- **Railway env vars are per-service.** When rotating a secret,
  update **both** Railway services AND the GitHub Actions repo
  secrets. The two stacks share no env state.
- **Both Railway services build from the same `Dockerfile`.** The
  per-service `railway.*.json` overrides only the `startCommand` and
  `cronSchedule`. If the Dockerfile breaks, both services fail to
  build — there's no independent failure isolation at the build
  layer (only at runtime).
- **`LINNWORKS_REFERENCE.md`** in the repo has the full Linnworks API
  working reference (auth, gotchas, the
  `Dashboards/ExecuteCustomPagedScript` form-encoding trick, etc.).
  Read it before writing new Linnworks endpoint code.

## Migration discipline

- **Never edit a migration that has already been applied to a live
  database.** Migrations are append-only — additions go in a new
  numbered file (`002_*.sql`, `003_*.sql`, ...). Editing an applied
  migration creates silent schema drift between code and database that
  PostgREST will report ambiguously (e.g. the `orders_skipped_empty`
  PGRST204 that masqueraded as a stale-cache problem for the whole
  Railway era — see backlog).

## Working method (Kevin's preference)

- Kevin uses Claude Code in the terminal for code generation.
- Chat-Claude is architect/reviewer.
- Chat writes prompts; Claude Code commits + pushes; Kevin runs
  workflows from the GitHub Actions tab and pastes logs back.
- Migration changes need manual paste into the Supabase SQL editor +
  `NOTIFY pgrst, 'reload schema';` after.
- GitHub Desktop for version control (not CLI git) when Kevin works
  the repo himself.
- Numbered update logs and step-by-step instructions preferred.
- Reliable solutions over fancy ones — was burned by macOS permission
  battles during a previous build.

## Architecture (current production shape)

```
              ┌────────────────────────────────────┐
              │           RAILWAY (cron)           │
              │                                    │
              │  ┌──────────────────────────────┐  │
              │  │ sync service                 │  │
              │  │ railway.sync.json            │  │
              │  │ */30 * * * *                 │  │
              │  │ python -m tools.sync_...     │  │
              │  └──────────────┬───────────────┘  │
              │                 │                  │
              │  ┌──────────────┴───────────────┐  │
              │  │ pull service                 │  │
              │  │ railway.pull.json            │  │
              │  │ */5 * * * *                  │  │
              │  │ python -m tools.pull_...     │  │
              │  └──────────────┬───────────────┘  │
              └─────────────────┼──────────────────┘
                                │
              ┌─────────────────┼──────────────────┐
              │     GITHUB ACTIONS (manual only)   │
              │                 │                  │
              │  workflow_dispatch:                │
              │   - sync (observe-mode debugging)  │
              │   - pull (observe-mode debugging)  │
              │   - probes (probe_* workflows)     │
              │   - wipe (destructive, manual)     │
              │  schedule: blocks commented out    │
              └─────────────────┼──────────────────┘
                                │
                                ▼
       ┌───────────────┐    ┌──────────────┐    ┌────────────┐
       │   LINNWORKS   │◄──►│   SUPABASE   │◄──►│   SQUARE   │
       │   (truth)     │    │  (audit log, │    │   (POS)    │
       │               │    │   sku map,   │    │            │
       │               │    │   watermarks)│    │            │
       └───────────────┘    └──────────────┘    └────────────┘
```

Both Railway services build from the shared `Dockerfile` at the repo
root. Each picks its own start command + cron schedule via its
`railway.*.json` config-as-code file.

## Stock decrement flow (live)

```
Square sale fires (POS at till)
    │
    ▼
order-pull cron picks it up (within 5 min, Railway)
    │
    ▼
Create Linnworks order (Source = "DIRECT", SKU-linked, paid)
    │
    ▼
Two-step mark-paid (ChangeOrderTag → ChangeStatus status=1)
    │ (Linnworks auto-decrements stock when the order lands and is paid)
    ▼
Order sits "open" in Linnworks for Kevin to process manually
    │
    │ (meanwhile, separately:)
    ▼
sync cron runs (every 30 min, Railway)
    │
    ▼
Reads current Linnworks stock for all SKUs
    │
    ▼
Pushes deltas to Square via Catalog + Inventory APIs
```

Linnworks is the only system that decrements stock on its own.
Square gets corrected to whatever Linnworks says. No two-way
decrement, no race conditions on the truth.

## Why these choices (preserved historical context)

### Why Railway cron and not GitHub Actions

GitHub Actions cron is free, but on the free tier the scheduler queue
silently throttles. `*/5` and `*/30` schedules were observed firing
30–90 minutes late under load — one observed: 69 min between a Square
sale at the till and the order landing in Linnworks. That blew the
acceptable staleness budget. Railway provides deterministic cron
scheduling and was the smallest blast-radius change: same Python code,
same Dockerfile, just a different scheduler.

GitHub Actions is retained for manual operations only:
- Manual `workflow_dispatch` runs of the sync tool (e.g., to force an
  immediate reconciliation, or to run in observe mode for debugging).
- Manual `workflow_dispatch` runs of the order-pull tool (same — for
  observe-mode debugging or one-off backfills).
- The wipe tool (intentionally only manual — destructive operation).
- Probes (`probe_*` workflows).

The `schedule:` blocks in both cron workflow YAMLs are commented out
to prevent double-runs while Railway is the source of truth.

(Originally we picked GHA because: free, every run is a clickable log
in the Actions tab — directly addressed the "I have no control over
the old app when it breaks" pain point. Cloudflare Workers cron has
been observed to silently stop firing for 24+ hours, which is why we
didn't pick that. Railway was deferred at the time because it cost
£4/month for nothing the GHA stack didn't already have — but the GHA
scheduler unreliability flipped that calculation once we needed the
*/5 cadence reliably.)

### Why Dockerfile and not Nixpacks / Railpack

Nixpacks is deprecated; Railway's current default is Railpack. Rather
than chase Railway's specific config formats (which keep changing), a
plain `Dockerfile` is vendor-neutral and predictable. Same image
locally, on Railway, or anywhere else with a Docker runtime. Easy
escape hatch if Railway pricing or reliability changes.

### Why Python and not Node

`LINNWORKS_REFERENCE.md` (sister project) is already written in
Python idioms. Square SDK for Python is well-maintained. GitHub
Actions runs Python natively without setup. (The Easyship app is a
separate project that stays in Node — no shared code.)

### Why Supabase tack-on rather than a new project

Kevin has used all his free Supabase project slots. Paying for
Supabase Pro to isolate this sync from another project isn't
justified ($25/mo for soft isolation). All this sync's tables are
prefixed `sq_*` to avoid collisions with the host project.

### Why two crons instead of one daemon

Independent failure domains. If `sync` breaks, orders still flow.
If `pull` breaks, stock still updates. No always-on service to
babysit. (Railway runs them as two separate services, so this
property carried forward from the GHA-era design unchanged.)

### Why a few minutes of staleness is acceptable

Northwest Guitars does ~100 orders/day across Shopify/eBay. A 5–20
minute stock-level lag is within tolerance — POS staff can see what's
physically on the shelf, and the count reconciles within ~30 minutes
worst-case (next sync tick). The double-sell race (last unit sold on
Shopify in the same window as a POS sale at the till) is rare and
would be handled the same way as today (apologise, refund). Webhooks
aren't worth the complexity for a once-a-month edge case.

## State management (Supabase tables)

All prefixed `sq_` to avoid collision with the host project.

| Table | Purpose |
|---|---|
| `sq_sku_map` | Spine intended for sync caching. One row per SKU; links Linnworks `StockItemId` to Square `catalog_object_id` + `variation_id`; caches `last_known_stock` and `last_known_price` for no-op skipping. **Not yet populated** — sync still re-derives the mapping from a catalog walk on every run. |
| `sq_sync_runs` | One row per cron execution (job_name, started_at, finished_at, status, items_processed/changed/errors, github_run_url). Used by `lib/db.py` `sync_run_start`/`sync_run_finish`. **Not the same as `sq_lw_sync_log`** — different shape and lifecycle. |
| `sq_square_orders_processed` | Idempotency for order-pull. Keyed on Square's `order_id`. |
| `sq_orders_pull_log` | Audit row per order-pull run (orders pulled / created / skipped / etc.). The `orders_skipped_empty` PGRST204 insert failure was schema drift, fixed by `002_*.sql` (pending live apply) — see backlog. |
| `sq_errors` | Per-error log for non-fatal failures during a sync run. |
| `sq_watermarks` | Key-value cursors (e.g. `square_orders_last_pulled_at`). |
| `sq_wipe_log` | Audit row per wipe-tool run (observe or write). UUID PK, mode, walked/deleted/failed/kept counts, error_summary. |
| `sq_lw_sync_log` | Audit row per Linnworks→Square sync-tool run. UUID PK, mode, pulled/walked/created/updated/stock_only/no_op/failed/duplicate-SKU counts, error_summary. |

Schema in `supabase/001_initial.sql`. Idempotent — every `CREATE`
uses `IF NOT EXISTS`. Re-run after any change, then
`NOTIFY pgrst, 'reload schema';`.

## Diagnostic-first development pattern

Per §10 of `LINNWORKS_REFERENCE.md`. Every endpoint shape we don't
already know gets probed by a standalone script in `probes/` before
production code is written against it. The probe script stays in the
repo permanently — it's how we re-validate when an API silently
changes. This pattern is what produced the locked-in `CreateOrders`
body shape, the mark-paid recipe, and the `product_type`-based
catalog discriminator.

## What was tried and rejected

- `Orders/SetPaymentStatus`, `Orders/AddOrderPayment`,
  `Orders/SetOrderPayment`, `Orders/PayOrder`,
  `Orders/SetOrderParkedStatus` — all 404 on this tenant. Don't
  re-test. Real mark-paid endpoint is `Orders/ChangeStatus` (after
  unparking via `Orders/ChangeOrderTag`).
- JSON-bodied call to `Orders/ChangeStatus` — returns 200 but
  silently no-ops. Endpoint requires `application/x-www-form-urlencoded`.
- Heuristic service detection via `available_for_booking` /
  `service_duration` / `team_member_ids` — unreliable on this tenant.
  Use `product_type == "APPOINTMENTS_SERVICE"` instead.
- **GitHub Actions free-tier scheduled cron** for the `*/5` order-pull
  cadence — scheduler queue silently throttles, ticks delivered 30–90
  min late. Migrated to Railway. GHA retained for manual runs only.
- **Nixpacks builder on Railway** — deprecated upstream. Switched to
  a plain Dockerfile for vendor neutrality.
