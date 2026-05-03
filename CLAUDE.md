# Linnworks ↔ Square Sync — Project State

## Current State (May 3, 2026)

**Project location**: `/Volumes/Music/Github/Square Linnworks Sync` (local),
`kevg1973/Square-Linnworks-Sync` (private GitHub repo).

**Goal**: Replace the legacy Square sync with a clean rebuild.
Linnworks is the source of truth (~100 orders/day across Shopify and
eBay). Square is used for POS + Appointments only. **Cutover before
the shop reopens Tuesday morning.**

**Stack**: Python 3.12, GitHub Actions cron, Supabase (project
`cart-upsell-tracker`, all tables prefixed `sq_*`), £0/month.

**Phase status**:

| Phase | What | Status |
|---|---|---|
| **0a** | Repo skeleton + auth smoke test                        | ✅ complete (2026-05-01) |
| **0b** | Diagnostic probes (Square scopes, Linnworks endpoints) | ✅ complete (2026-05-02) — 4/4 probes green, mark-paid recipe locked in |
| **1**  | Wipe + rebuild Square's retail catalog from Linnworks  | ⏳ tools built and observe-verified, **write-mode runs pending** |
| 2      | Stock-push cron (Linnworks → Square, every 15 min)     | not started |
| 3      | Order-pull cron (Square → Linnworks, every 5 min)      | not started |
| 4      | Operational dashboard                                   | not started |

## What's been built and tested

### Wipe tool — ✅ COMPLETE, observe-verified

`tools/wipe_square_items.py` + `.github/workflows/wipe-square-items.yml`.

- Walks the entire Square catalog, classifies each item by
  `item_data.product_type`.
- Observe run reports: **would delete 7,381 REGULAR retail items, keep
  9 APPOINTMENTS_SERVICE**. Anything with an unknown/missing
  `product_type` is skipped and reported (never deleted).
- `--write` flag required to actually delete; default is observe.
  Workflow `mode` input dropdown also defaults to `observe`.
- `--limit N` for staged write runs.
- Audit log to `sq_wipe_log` (one row per run, observe or write).

### Sync tool — ✅ OBSERVE WORKING, write-mode pending

`tools/sync_linnworks_to_square.py` +
`.github/workflows/sync-linnworks-to-square.yml`.

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

**Latest observe-run breakdown (pre-wipe)**:

| Metric | Value |
|---|---|
| Linnworks items pulled                | 4,193 |
| Square REGULAR SKUs walked            | 6,354 |
| Duplicate Square SKUs ignored         | 1,027 |
| Would CREATE                          | 105 |
| Would UPDATE (mostly price fixes)     | 400 |
| Would STOCK_ONLY                      | 2,338 |
| Would NO_OP                           | 1,350 |

**Recent fixes (since the latest observe run)**:

- Linnworks pagination now terminates cleanly via partial-page
  detection (and treats a follow-up HTTP 400 as expected
  end-of-catalog only when the previous page was already partial).
  Was previously propagating the 400 from page 22 as an error.
  Commit `4787c4c`.
- The final `=== SYNC COMPLETE: ===` summary line and the
  `sq_lw_sync_log` audit row now use plan-phase action counts
  (`len(creates)` etc.) regardless of mode, with `failed` tracked
  separately. The previous version hardcoded zeros in observe mode
  and double-counted failures in write mode. Commit `cde7412`.

A re-run in observe mode is the next sanity check before write-mode
can be trusted — the summary line should now match the PLAN block
exactly.

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
  the cron lands).
- **Square location ID**: `L74KSP08AJ2GH` (Northwest Guitars). Pinned
  in code — will be promoted to a `SQUARE_LOCATION_ID` env var when
  Phase 2 cron lands.
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

### `Orders/CreateOrders` (Phase 3 prep, locked in by probe)

- `POST /api/Orders/CreateOrders` — JSON body `{"orders": [<order>]}`.
  Plural; even for a single order. Response is a bare JSON array of
  `pkOrderID` strings (not wrapped in `{Orders: [...]}`).
- Required fields: `Source`, `SubSource`, `ReferenceNumber`,
  `ExternalReferenceNumber`, `ReceivedDate`, `DispatchBy`,
  `LocationId`, `Currency`, `OrderItems`, `DeliveryAddress`,
  `BillingAddress`. The address must be named `DeliveryAddress`,
  NOT `ShippingAddress`.
- **Dedup key** is `(Source, SubSource, ReferenceNumber)`. Phase 3's
  order-pull should derive `ReferenceNumber` deterministically from
  Square's order id so retries are naturally idempotent.
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

## Required GitHub Actions secrets (all 7 set)

```
LINNWORKS_APP_ID
LINNWORKS_APP_SECRET
LINNWORKS_TOKEN
SQUARE_ACCESS_TOKEN
SQUARE_APPLICATION_ID
SUPABASE_URL              (https://miicdzowfzxffnorlqzp.supabase.co)
SUPABASE_SERVICE_KEY
```

All seven go into GitHub repo Settings → Secrets and variables →
Actions as repository secrets. None are committed to the repo.

## Repo structure

```
Square-Linnworks-Sync/
├── .github/workflows/
│   ├── smoke-test.yml
│   ├── probe-square-scopes.yml
│   ├── probe-square-catalog.yml
│   ├── probe-square-duplicates.yml
│   ├── probe-linnworks-create-orders.yml
│   ├── probe-linnworks-mark-paid.yml
│   ├── probe-supabase-write-pattern.yml
│   ├── wipe-square-items.yml              ✅
│   └── sync-linnworks-to-square.yml       ✅ (observe verified)
├── lib/                  config.py, linnworks.py, square.py, db.py
├── probes/               probe_*.py (Phase 0b artefacts)
├── tools/
│   ├── __init__.py
│   ├── wipe_square_items.py               ✅
│   └── sync_linnworks_to_square.py        ✅ (observe verified)
├── supabase/001_initial.sql               sq_* tables incl. sq_wipe_log + sq_lw_sync_log
├── CLAUDE.md
├── LINNWORKS_REFERENCE.md
├── DISCOVERIES.md
└── requirements.txt
```

## Cutover plan (target: Monday before EOD)

**Precondition** (do this once before any of the steps below):
paste the contents of `supabase/001_initial.sql` into the Supabase
SQL Editor and run, then run `NOTIFY pgrst, 'reload schema';` in a
fresh query. The migration is idempotent (every CREATE uses
`IF NOT EXISTS`), and the NOTIFY is what makes PostgREST's API
actually see new tables — without it, audit-row inserts fail with
`PGRST205` even when the table exists in the database.

1. Re-run **Sync — Linnworks → Square** in observe mode. Verify the
   summary line now matches the PLAN block exactly (post-`cde7412`
   fix), and the breakdown still looks sensible (≈4,193 Linnworks
   items, mostly CREATE after wipe).
2. Maintenance window opens (shop closed).
3. Run **Wipe — Square retail items** with `mode=write`. Deletes
   ≈7,381 retail items, keeps 9 services. Audit row in `sq_wipe_log`.
4. Verify Square dashboard shows only services in the Item library.
5. Run **Sync — Linnworks → Square** with `mode=write`. Creates
   ≈4,193 retail items and pushes stock from Linnworks. Audit row
   in `sq_lw_sync_log`.
6. Verify Square dashboard populated correctly.
7. Kill the legacy sync app.
8. Monitor for an hour.
9. Tuesday morning: shop reopens, till works.

## Open issues / next session

**Pre-cutover checklist** (must clear before any `--write` run):

- **Run the migration.** Editing `supabase/001_initial.sql` in the
  repo only changes the file — it does not touch the live database.
  `sq_lw_sync_log` was added to the SQL on 2026-05-03 but the
  CREATE wasn't actually executed against Supabase until the
  audit-insert started failing with `PGRST205`. Always do **both**
  in the Supabase SQL Editor after a migration change: paste the
  full file → Run, then run `NOTIFY pgrst, 'reload schema';` so
  PostgREST sees the new tables. The schema-cache `NOTIFY` alone
  doesn't create anything.
- **Confirm the observe-run summary line matches the PLAN block**
  after the `cde7412` counter fix. If they don't match for any
  reason, do not run `--write` yet.

**Deferred (out of cutover scope)**:

- **Image sync.** Basic SKU/name/price/stock first; product
  pictures are a follow-up after cutover.
- **Per-supplier code cache, sales velocity ingest from Script 47**,
  etc.
- **Promote `SQUARE_LOCATION_ID` to an env var.** Currently
  hardcoded as `L74KSP08AJ2GH` across the tools. Lift to config
  when the Phase 2 cron lands.
- **`sq_sku_map` is unused.** Schema exists but nothing writes to
  it yet; the sync re-derives the SKU↔catalog-id mapping on every
  observe/write run by walking Square's catalog. Phase 2 cron will
  populate this table to skip no-op writes between runs.

**Phase 2/3 still to build**:

- **Phase 2 — stock-push cron** (`stock-push.yml`, every 15 min,
  Linnworks → Square). The Phase 1 sync tool is a one-shot manual
  rebuild; the cron is the recurring delta sync. Most of the
  Linnworks pull / Square upsert / inventory-push code can lift
  directly from the sync tool.
- **Phase 3 — order-pull cron** (`order-pull.yml`, every 5 min,
  Square → Linnworks). Creates Linnworks orders from Square POS
  sales. Mark-paid recipe is locked in (see *Discoveries*) — the
  build is mostly: pull Square orders since watermark, idempotently
  create Linnworks orders, unpark via `ChangeOrderTag`, mark paid
  via `ChangeStatus(status=1)`, advance watermark.
- **Phase 4 — operational dashboard.** Reads `sq_sync_runs`,
  `sq_errors`, `sq_wipe_log`, `sq_lw_sync_log`, surfaces last-run
  status and recent errors. Shape TBD.
- **Phase 5 — orphan-deletion path on reconciliation** with a
  confirm step.

## Operational gotchas

- **Repo lives on external SSD** `/Volumes/Music/`. Must be mounted
  to commit (not to run workflows — those run on GitHub-hosted
  runners regardless of local mount state).
- **macOS leaks `._*` AppleDouble files** when copying onto FAT/exFAT
  volumes. `.gitignore` should swallow them; if any sneak in, delete
  with `find . -name '._*' -delete`.
- **Supabase free Nano projects pause after 7 days inactivity.** If a
  workflow run starts failing with connection errors, check the
  Supabase dashboard and unpause if needed.
- **Linnworks**: rate limit ~1 req/sec, auth header is the raw token
  (no `Bearer` prefix), 401 → re-auth + retry once.
- **After modifying `supabase/001_initial.sql`**: paste into the
  Supabase SQL editor and run it (idempotent — every CREATE uses
  `IF NOT EXISTS`), **then** run `NOTIFY pgrst, 'reload schema';` so
  PostgREST sees new tables. Without the NOTIFY, audit inserts will
  fail with `PGRST205` for ~minutes until the cache naturally
  refreshes.
- **`LINNWORKS_REFERENCE.md`** in the repo has the full Linnworks API
  working reference (auth, gotchas, the `Dashboards/ExecuteCustomPagedScript`
  form-encoding trick, etc.). Read it before writing new Linnworks
  endpoint code.

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

## Architecture (the eventual shape, post-cutover)

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

The Phase 1 wipe + sync tools are the manual one-shot equivalent of
what the Phase 2 cron will eventually do automatically. Once the
catalog is rebuilt cleanly, Phase 2 takes over for the recurring
delta sync.

## Stock decrement flow (the eventual loop, post-Phase-3)

```
Square sale fires (POS at till)
    │
    ▼
order-pull cron picks it up (within 5 min)
    │
    ▼
Create Linnworks order (Source = "DIRECT")
    │
    ▼
Two-step mark-paid (ChangeOrderTag → ChangeStatus status=1)
    │ (Linnworks auto-decrements stock when the order lands and is paid)
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

Linnworks is the only system that decrements stock on its own.
Square gets corrected to whatever Linnworks says. No two-way
decrement, no race conditions on the truth.

## Why these choices (preserved historical context)

### Why GitHub Actions and not Railway / Cloudflare

GitHub Actions cron is genuinely free for this workload. Every run
is a clickable log in the Actions tab — directly addresses the "I
have no control over the old app when it breaks" pain point.
Cloudflare Workers cron has been observed to silently stop firing
for 24+ hours; Railway Hobby would work but costs £4/month for
nothing this stack doesn't already have.

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

Independent failure domains. If `stock-push` breaks, orders still
flow. If `order-pull` breaks, stock still updates. No always-on
service to babysit.

### Why a few minutes of staleness is acceptable

Northwest Guitars does ~100 orders/day across Shopify/eBay. A 5–20
minute stock-level lag is within tolerance — POS staff can see what's
physically on the shelf, and the count reconciles within 20 minutes
worst-case. The double-sell race (last unit sold on Shopify in the
same window as a POS sale at the till) is rare and would be handled
the same way as today (apologise, refund). Webhooks aren't worth the
complexity for a once-a-month edge case.

## State management (Supabase tables)

All prefixed `sq_` to avoid collision with the host project.

| Table | Purpose |
|---|---|
| `sq_sku_map` | Spine for the eventual cron. One row per SKU; links Linnworks `StockItemId` to Square `catalog_object_id` + `variation_id`; caches `last_known_stock` and `last_known_price` for no-op skipping. Not yet populated — Phase 2 cron will write to this. |
| `sq_sync_runs` | One row per cron execution (job_name, started_at, finished_at, status, items_processed/changed/errors, github_run_url). Used by `lib/db.py` `sync_run_start`/`sync_run_finish`. **Not the same as `sq_lw_sync_log`** — different shape and lifecycle. |
| `sq_square_orders_processed` | Audit + idempotency for order-pull. Keyed on Square's `order_id`. Phase 3. |
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
