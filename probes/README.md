# probes/

Diagnostic, side-effect-aware scripts that probe API endpoints we
haven't locked down yet. Per §10 of `LINNWORKS_REFERENCE.md`, every
endpoint shape we don't already know gets probed by a standalone
script before production code is written against it.

Each probe has its own `workflow_dispatch` workflow so it can be run
on-demand from the GitHub Actions tab without disturbing anything
else. Findings are pasted into `DISCOVERIES.md` at the repo root.

## Phase 0b probes

| Script | Workflow | What it probes |
|---|---|---|
| `probe_square_scopes.py` | `probe-square-scopes.yml` | Whether the Square access token has the six scopes we need (`ITEMS_READ/WRITE`, `INVENTORY_READ/WRITE`, `ORDERS_READ`, plus location read). Side-effect-free — never modifies data. |
| `probe_linnworks_create_orders.py` | `probe-linnworks-create-orders.yml` | The wire format that `Orders/CreateOrders` (plural) accepts on this tenant, with all mandatory fields filled in. Creates a real test order, prints the `pkOrderID`, then deletes it in a `finally` block. v1 (`probe_linnworks_create_order.py`, singular) targeted `Orders/CreateNewOrder` — wrong endpoint — and is kept as a deprecation stub. |
| `probe_linnworks_mark_paid.py` | `probe-linnworks-mark-paid.yml` | The mechanism for marking an order as paid without dispatching. Creates a test order, tries each candidate path, verifies via readback, then deletes the order. |
| `probe_supabase_write_pattern.py` | `probe-supabase-write-pattern.yml` | The four supabase-py write patterns used in production: upsert with `on_conflict`, batch insert with `jsonb`, watermark round-trip, filtered query. Cleans up its own rows. |

## Test-data markers

Probes that create real data in Linnworks or Supabase tag every row /
order they create with a recognisable marker so a failed cleanup is
manually recoverable:

- **Linnworks orders** — customer name `[PROBE-CLEANUP-FAILED-{ts}]`,
  line-item SKU `__PROBE_TEST_DO_NOT_USE__` (a deliberately invalid
  SKU that won't match any real inventory).
- **Supabase rows** — SKU `__PROBE_TEST_DO_NOT_USE__`, job name
  `__probe_test__`, watermark key `__probe_test_watermark__`.

If a probe fails between creating a test row and cleaning it up, it
will print the orphaned identifier prominently in the run log and the
final cleanup status will say so loudly. Search by marker to clean up
manually.

## Re-running probes

All probes are designed to be re-runnable: a leftover row from a prior
failed run won't break the next run. Probes that create data either
generate a fresh identifier each run, or wipe by-marker before
starting.
