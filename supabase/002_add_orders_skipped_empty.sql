-- Add missing orders_skipped_empty counter to the pull-log audit table.
-- Was referenced in the code but never landed in the live schema, so
-- PostgREST rejected every sq_orders_pull_log insert with PGRST204 for
-- the whole Railway era. The column is present in 001_initial.sql, but
-- that migration was applied to the live DB before the column was added,
-- so this follow-up brings the live schema in line.
--
-- Idempotent (IF NOT EXISTS) — safe to re-run. NOT NULL DEFAULT 0 matches
-- the convention used by the other counters in this table.
ALTER TABLE sq_orders_pull_log
  ADD COLUMN IF NOT EXISTS orders_skipped_empty INTEGER NOT NULL DEFAULT 0;

NOTIFY pgrst, 'reload schema';
