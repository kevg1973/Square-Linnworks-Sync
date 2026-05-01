# probes/

Diagnostic, read-only scripts that probe API endpoints we haven't
locked down yet. Per §10 of `LINNWORKS_REFERENCE.md`, every endpoint
shape we don't already know gets probed by a standalone script before
production code is written against it.

**This directory is empty in Phase 0a.** It populates in Phase 0b with
four scripts:

- `square_scopes.py` — calls each Square endpoint we'll need and
  reports whether the existing access token has sufficient scopes.
- `square_locations.py` — lists Square locations and prints the one
  matching "Northwest Guitars" so we know which `location_id` to pin
  for inventory writes.
- `linnworks_create_order.py` — probes `Orders/CreateNewOrder` with
  multiple body shapes (flat / request-wrapped) and reports the
  shape that returns 200 on Northwest Guitars' tenant.
- `linnworks_mark_paid.py` — probes whether marking an order as paid
  is a field on `CreateNewOrder` itself or a separate call.

Each probe has its own `workflow_dispatch` workflow so it can be run
on-demand from the GitHub Actions tab without disturbing the cron
jobs (which don't exist yet anyway).

Findings are written to `DISCOVERIES.md` at the repo root and
committed back so future-Kevin (or future-Claude) doesn't have to
re-derive them.
