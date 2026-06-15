-- Failed-orders retry table. Each row is a Square order that the order-pull
-- cron tried to create in Linnworks and failed. The cron re-attempts these
-- every run until success (row deleted) or escalation to stuck at attempts
-- >= 5 (row stays, stuck = TRUE, no more auto-retry, email sent once).
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

-- Augment the audit log with retry-visible counters.
ALTER TABLE sq_orders_pull_log
    ADD COLUMN IF NOT EXISTS orders_retried INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS orders_retry_succeeded INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS stuck_orders_count INTEGER NOT NULL DEFAULT 0;

NOTIFY pgrst, 'reload schema';
