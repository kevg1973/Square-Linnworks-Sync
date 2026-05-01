# DISCOVERIES.md

A permanent, committed record of facts learned by running the Phase 0b
diagnostic probes against Northwest Guitars' real Linnworks tenant and
real Square account.

This file is **the contract** between probe scripts and production code.
Once a fact is locked in here, the production code (Phases 1–4) reads
the working endpoint shapes / scopes / IDs from this doc rather than
re-deriving them.

How this file gets populated: Kevin runs each probe from the GitHub
Actions tab (`workflow_dispatch`), copies the lines that start with
`=== DISCOVERY: ===` from the run log, pastes them into the relevant
section below, and commits.

When a probe is re-run later (e.g. after a Square scope change or a
Linnworks API tweak), update the relevant section in place. Keep dated
entries when behaviour changes meaningfully.

---

## 1. Square API scopes

**Probe**: `probes/probe_square_scopes.py`
**Workflow**: `.github/workflows/probe-square-scopes.yml`
**Status**: [run probe to populate]

For each Square endpoint we'll need, the probe reports OK or captures
the `INSUFFICIENT_SCOPES` error. Production code (stock-push,
order-pull, reconciliation) cannot proceed if any required scope is
missing.

| Endpoint | Required scope(s) | Status |
|---|---|---|
| `GET /v2/locations` | `MERCHANT_PROFILE_READ` | [run probe] |
| `GET /v2/catalog/list` | `ITEMS_READ` | [run probe] |
| `POST /v2/catalog/upsert-catalog-object` | `ITEMS_WRITE` | [run probe] |
| `POST /v2/inventory/counts/batch-retrieve` | `INVENTORY_READ` | [run probe] |
| `POST /v2/inventory/changes/batch-create` | `INVENTORY_WRITE` | [run probe] |
| `POST /v2/orders/search` | `ORDERS_READ` | [run probe] |

**If any scope is missing**: open the Square developer dashboard for
this app, edit OAuth scopes (or, for a personal access token, ensure
the token grants the missing permission), re-issue the access token,
update the GitHub secret, and re-run the probe.

---

## 2. Square location ID

**Probe**: `probes/probe_square_scopes.py` (covers locations as part of
its `GET /v2/locations` test)
**Status**: known — `L74KSP08AJ2GH` (Northwest Guitars), confirmed
2026-05-01 from the Phase 0a smoke test.

This is the location ID used for all inventory reads/writes against
the physical shop. It will be promoted to a `SQUARE_LOCATION_ID`
GitHub secret when Phase 2 (stock-push) lands.

---

## 3. Linnworks `Orders/CreateOrders` body shape

**Probe**: `probes/probe_linnworks_create_orders.py` (v2)
**Workflow**: `.github/workflows/probe-linnworks-create-orders.yml`
**Status**: [run probe to populate]

The v1 probe (`probes/probe_linnworks_create_order.py`, deprecated and
kept as a stub) targeted the wrong endpoint: `Orders/CreateNewOrder`
creates an empty draft order, not the fully-formed order we need.
v2 targets `Orders/CreateOrders` (plural) with all documented
mandatory fields per
https://help.linnworks.com/support/solutions/articles/7000013635 :

- `Source`           = "DIRECT"
- `SubSource`        = "PROBE_TEST" (probe) / "SQUARE_POS" (production)
- `ReferenceNumber`  = unique-per-source-and-subsource string (Linnworks dedupes on this)
- `ReceivedDate`     = ISO datetime
- `DispatchBy`       = ISO datetime, > now
- `OrderItems`       = array of `{SKU, Qty, PricePerUnit, ItemTitle, ...}`
- `DeliveryAddress`  = address dict (note: must be named `DeliveryAddress`, NOT `ShippingAddress`)
- `BillingAddress`   = same shape as DeliveryAddress
- `LocationId`       = stock-location UUID
- `Currency`         = "GBP"

The probe tries the JSON wire format first, then falls back to
form-encoded `orders=<json string of array>`. Every full request and
response body is logged so 400 details are readable.

**Working wire format**: [run probe — paste the attempt label]

**Working order body** (mandatory fields filled in): [run probe to
populate]

```json
[run probe to populate]
```

**Response shape** (key path to `pkOrderID`): [run probe to populate]

**Cleanup endpoint** (probe deletes its own test orders): [run probe
to populate — first candidate tried is `Orders/DeleteOrder` with
`{"orderId": "<uuid>"}`]

---

## 4. Linnworks "mark as paid (no dispatch)" mechanism

**Probe**: `probes/probe_linnworks_mark_paid.py`
**Workflow**: `.github/workflows/probe-linnworks-mark-paid.yml`
**Status**: [run probe to populate]

Order-pull (Phase 3) creates Linnworks orders from Square POS sales.
Square already took the money at the till, so the Linnworks order must
be marked **paid** but **not** dispatched (Kevin processes dispatch
manually). This may be a field on `CreateNewOrder` itself, or a
separate call (e.g. `Orders/SetOrderPaymentStatus`,
`Orders/PayOrder`). The probe finds out.

**Working mechanism**: [run probe to populate]

**Working body shape**: [run probe to populate]

```json
[run probe to populate]
```

**Verification readback** (which field on `Orders/GetOrdersById`
confirms paid-without-dispatch): [run probe to populate]

---

## 5. Supabase write patterns

**Probe**: `probes/probe_supabase_write_pattern.py`
**Workflow**: `.github/workflows/probe-supabase-write-pattern.yml`
**Status**: [run probe to populate]

Validates the four supabase-py patterns used in production code:

1. **Upsert with `on_conflict`** on `sq_sku_map` (keyed on `sku`).
2. **Batch insert** of multiple rows into `sq_errors` in one call,
   with `jsonb` `context`.
3. **Watermark read/write round-trip** on `sq_watermarks`.
4. **Filtered query** on `sq_sync_runs` by `status` + `started_at`.

**Result**: [run probe to populate — expect "all four patterns
worked" or a list of which pattern failed and why]

The probe cleans up after itself by deleting all rows tagged with the
`__probe_test__` marker (job name, sku, watermark key) at the end. If
cleanup fails, the run log will name what was left behind.

---

## How probes communicate findings

Every probe prints lines starting with `=== DISCOVERY: ===` to stdout.
After running a probe, search the GitHub Actions run log for that
prefix and copy the lines into the relevant section above.

Example log fragment:

```
=== DISCOVERY: ITEMS_READ scope OK on /v2/catalog/list ===
=== DISCOVERY: ITEMS_WRITE scope OK on /v2/catalog/upsert-catalog-object ===
=== DISCOVERY: INVENTORY_READ scope MISSING on /v2/inventory/counts/batch-retrieve — code: INSUFFICIENT_SCOPES ===
```

That single grep is the entire workflow for moving findings from
"observed in CI" to "checked into the repo".
