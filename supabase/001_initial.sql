-- linnworks-square-sync — initial schema
--
-- All tables prefixed `sq_` so they coexist with whatever else is in
-- the host Supabase project. Run this against your existing Supabase
-- project via SQL Editor → New query → paste → Run.
--
-- Safe to re-run: every CREATE uses IF NOT EXISTS.

-- ============================================================================
-- sq_sku_map
--   The spine of the integration. One row per SKU. Links Linnworks'
--   StockItemId to Square's catalog_object_id + variation_id, and
--   caches last-known stock + price so we can skip no-op writes on
--   stock-push runs.
-- ============================================================================
CREATE TABLE IF NOT EXISTS sq_sku_map (
    sku                  TEXT PRIMARY KEY,
    linnworks_item_id    UUID,
    square_catalog_id    TEXT,
    square_variation_id  TEXT,
    last_known_stock     INTEGER,
    last_known_price     NUMERIC(12, 2),
    last_pushed_at       TIMESTAMPTZ,
    active               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sq_sku_map_active ON sq_sku_map (active)
    WHERE active = TRUE;
CREATE INDEX IF NOT EXISTS idx_sq_sku_map_square_catalog
    ON sq_sku_map (square_catalog_id);

-- ============================================================================
-- sq_sync_runs
--   One row per cron execution. Lets the dashboard show "last
--   successful run", "errors in last 24h", etc.
-- ============================================================================
CREATE TABLE IF NOT EXISTS sq_sync_runs (
    id                BIGSERIAL PRIMARY KEY,
    job_name          TEXT NOT NULL,           -- 'stock-push', 'order-pull', 'reconcile', 'smoke-test'
    started_at        TIMESTAMPTZ NOT NULL,
    finished_at       TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'running',  -- 'running', 'success', 'partial', 'failed'
    items_processed   INTEGER NOT NULL DEFAULT 0,
    items_changed     INTEGER NOT NULL DEFAULT 0,
    errors_count      INTEGER NOT NULL DEFAULT 0,
    github_run_url    TEXT
);

CREATE INDEX IF NOT EXISTS idx_sq_sync_runs_job_started
    ON sq_sync_runs (job_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sq_sync_runs_status
    ON sq_sync_runs (status, started_at DESC);

-- ============================================================================
-- sq_square_orders_processed
--   Audit log + idempotency for order-pull. Keyed on Square's
--   order_id. If we see the same Square order again in a future run,
--   we skip it.
-- ============================================================================
CREATE TABLE IF NOT EXISTS sq_square_orders_processed (
    square_order_id     TEXT PRIMARY KEY,
    linnworks_order_id  UUID,
    total               NUMERIC(12, 2),
    customer_name       TEXT,
    processed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status              TEXT NOT NULL,        -- 'created', 'duplicate', 'failed'
    error_message       TEXT
);

CREATE INDEX IF NOT EXISTS idx_sq_orders_processed_at
    ON sq_square_orders_processed (processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_sq_orders_status
    ON sq_square_orders_processed (status, processed_at DESC);

-- ============================================================================
-- sq_errors
--   Per-error log for non-fatal failures during a sync run. The
--   sync_run row records the count; this table records the details.
-- ============================================================================
CREATE TABLE IF NOT EXISTS sq_errors (
    id            BIGSERIAL PRIMARY KEY,
    job_name      TEXT NOT NULL,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    message       TEXT NOT NULL,
    context       JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_sq_errors_recent
    ON sq_errors (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_sq_errors_job
    ON sq_errors (job_name, occurred_at DESC);

-- ============================================================================
-- sq_watermarks
--   Key-value store for cursors. Each cron uses this to remember
--   "what's the last thing I successfully processed" so it can resume
--   on the next run.
--
--   Known keys (will grow):
--     square_orders_last_pulled_at  ISO timestamp of last Square order pulled
-- ============================================================================
CREATE TABLE IF NOT EXISTS sq_watermarks (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- sq_wipe_log
--   Audit log for tools/wipe_square_items.py. One row per run
--   (observe or write), so we can always look up "when was the last
--   wipe and what did it touch?". Mode='observe' rows are dry runs
--   that didn't delete anything; mode='write' rows record actual
--   deletions.
-- ============================================================================
CREATE TABLE IF NOT EXISTS sq_wipe_log (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode                  TEXT NOT NULL,        -- 'observe' | 'write'
    items_walked          INTEGER NOT NULL DEFAULT 0,
    items_deleted         INTEGER NOT NULL DEFAULT 0,
    items_failed          INTEGER NOT NULL DEFAULT 0,
    items_kept_services   INTEGER NOT NULL DEFAULT 0,
    error_summary         TEXT
);

CREATE INDEX IF NOT EXISTS idx_sq_wipe_log_run_at
    ON sq_wipe_log (run_at DESC);

-- ============================================================================
-- sq_lw_sync_log
--   Audit log for tools/sync_linnworks_to_square.py. One row per run
--   (observe or write). Mirrors sq_wipe_log's shape — UUID primary
--   key, run_at, mode, then per-category counters and an error
--   summary string. This is separate from sq_sync_runs (which is the
--   cron job-tracking table used by lib/db.py).
-- ============================================================================
CREATE TABLE IF NOT EXISTS sq_lw_sync_log (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode                     TEXT NOT NULL,        -- 'observe' | 'write'
    linnworks_items_pulled   INTEGER NOT NULL DEFAULT 0,
    square_items_walked      INTEGER NOT NULL DEFAULT 0,
    created                  INTEGER NOT NULL DEFAULT 0,
    updated                  INTEGER NOT NULL DEFAULT 0,
    stock_only               INTEGER NOT NULL DEFAULT 0,
    no_op                    INTEGER NOT NULL DEFAULT 0,
    failed                   INTEGER NOT NULL DEFAULT 0,
    duplicate_skus           INTEGER NOT NULL DEFAULT 0,
    error_summary            TEXT
);

CREATE INDEX IF NOT EXISTS idx_sq_lw_sync_log_run_at
    ON sq_lw_sync_log (run_at DESC);

-- ============================================================================
-- sq_orders_pull_log
--   Audit log for tools/pull_square_orders_to_linnworks.py. One row
--   per run (observe or write). Mirrors sq_wipe_log / sq_lw_sync_log
--   shape — UUID primary key, run_at, mode, then per-category
--   counters and an error summary string. Records the watermark
--   before/after so we can reconstruct windows from the log alone.
-- ============================================================================
CREATE TABLE IF NOT EXISTS sq_orders_pull_log (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode               TEXT NOT NULL,        -- 'observe' | 'write'
    watermark_before   TIMESTAMPTZ,
    watermark_after    TIMESTAMPTZ,
    orders_fetched     INTEGER NOT NULL DEFAULT 0,
    orders_processed     INTEGER NOT NULL DEFAULT 0,  -- all 3 Linnworks steps + bookkeeping landed
    orders_skipped       INTEGER NOT NULL DEFAULT 0,  -- already in sq_square_orders_processed
    orders_skipped_empty INTEGER NOT NULL DEFAULT 0,  -- Square returned 0 line items
    orders_failed        INTEGER NOT NULL DEFAULT 0,  -- one per order; any of the 3 Linnworks steps failed
    orders_created       INTEGER NOT NULL DEFAULT 0,  -- step 1 (CreateOrders) succeeded
    orders_unparked      INTEGER NOT NULL DEFAULT 0,  -- step 2 (ChangeOrderTag) succeeded
    orders_marked_paid   INTEGER NOT NULL DEFAULT 0,  -- step 3 (ChangeStatus) succeeded
    error_summary        TEXT
);

-- Backfill columns on installs that pre-date the three-step counters
-- and the empty-order skip counter. No production data to migrate —
-- these are pure additions.
ALTER TABLE sq_orders_pull_log
    ADD COLUMN IF NOT EXISTS orders_created INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sq_orders_pull_log
    ADD COLUMN IF NOT EXISTS orders_unparked INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sq_orders_pull_log
    ADD COLUMN IF NOT EXISTS orders_marked_paid INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sq_orders_pull_log
    ADD COLUMN IF NOT EXISTS orders_skipped_empty INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_sq_orders_pull_log_run_at
    ON sq_orders_pull_log (run_at DESC);

-- ============================================================================
-- updated_at triggers
--   Cheap automatic updated_at on sku_map. We don't bother with
--   triggers on the other tables because they're append-only or have
--   their own explicit timestamps.
-- ============================================================================
CREATE OR REPLACE FUNCTION sq_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS sq_sku_map_updated_at ON sq_sku_map;
CREATE TRIGGER sq_sku_map_updated_at
    BEFORE UPDATE ON sq_sku_map
    FOR EACH ROW
    EXECUTE FUNCTION sq_set_updated_at();

-- ============================================================================
-- sq_orders_failed  (added by migration 003_failed_orders_retry.sql)
--   Failed-orders retry table. One row per Square order the order-pull
--   cron tried to create in Linnworks and failed. Re-attempted every run
--   until success (row deleted) or escalation to stuck at attempts >= 5
--   (row stays, stuck = TRUE, no more auto-retry, email sent once).
--   Decouples failed-order handling from the watermark — the watermark
--   can advance past a failed order without stranding it.
--
--   New table appended as its own block (migration discipline: never edit
--   an already-applied CREATE TABLE). Mirrors 003_failed_orders_retry.sql.
-- ============================================================================
CREATE TABLE IF NOT EXISTS sq_orders_failed (
    square_order_id    TEXT PRIMARY KEY,
    first_failed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts           INTEGER NOT NULL DEFAULT 1,
    last_error         TEXT,
    square_order_json  JSONB,
    stuck              BOOLEAN NOT NULL DEFAULT FALSE,
    stuck_notified_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sq_orders_failed_stuck
    ON sq_orders_failed (stuck, last_attempted_at);

-- Retry-visible counters on the pull-log audit table (migration 003).
ALTER TABLE sq_orders_pull_log
    ADD COLUMN IF NOT EXISTS orders_retried INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sq_orders_pull_log
    ADD COLUMN IF NOT EXISTS orders_retry_succeeded INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sq_orders_pull_log
    ADD COLUMN IF NOT EXISTS stuck_orders_count INTEGER NOT NULL DEFAULT 0;
