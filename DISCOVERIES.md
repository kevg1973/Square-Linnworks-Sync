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

**Phase 0b status**: 3 of 4 probes complete (Square scopes, Supabase
write patterns, Linnworks CreateOrders). Mark-paid probe deferred —
all six candidate endpoints returned 404 on this tenant; the actual
endpoint needs to be researched from apidocs.linnworks.net before the
next probe attempt.

---

## 1. Square API scopes

**Probe**: `probes/probe_square_scopes.py`
**Workflow**: `.github/workflows/probe-square-scopes.yml`
**Status**: ✅ confirmed 2026-05-01 — all six required scopes present.

| Endpoint | Required scope | Status |
|---|---|---|
| `GET  /v2/locations`                       | `MERCHANT_PROFILE_READ` | ✅ OK |
| `GET  /v2/catalog/list`                    | `ITEMS_READ`            | ✅ OK |
| `POST /v2/catalog/upsert-catalog-object`   | `ITEMS_WRITE`           | ✅ OK |
| `POST /v2/inventory/counts/batch-retrieve` | `INVENTORY_READ`        | ✅ OK |
| `POST /v2/inventory/changes/batch-create`  | `INVENTORY_WRITE`       | ✅ OK |
| `POST /v2/orders/search`                   | `ORDERS_READ`           | ✅ OK |

The Square access token in the `SQUARE_ACCESS_TOKEN` secret has full
permission for everything stock-push, order-pull, and reconciliation
will need. No re-issue required.

---

## 2. Square location ID

**Probe**: `probes/probe_square_scopes.py` (covers locations as part of
its `GET /v2/locations` test)
**Status**: ✅ confirmed — `L74KSP08AJ2GH` (Northwest Guitars).

This is the location ID used for all inventory reads/writes against
the physical shop. It will be promoted to a `SQUARE_LOCATION_ID`
GitHub secret when Phase 2 (stock-push) lands.

---

## 3. Linnworks `Orders/CreateOrders` body shape

**Probe**: `probes/probe_linnworks_create_orders.py` (v3)
**Workflow**: `.github/workflows/probe-linnworks-create-orders.yml`
**Status**: ✅ confirmed end-to-end on 2026-05-02 (create + cleanup).

The v1 probe (`probes/probe_linnworks_create_order.py`, deprecated and
kept as a stub) targeted the wrong endpoint: `Orders/CreateNewOrder`
creates an empty draft order. v2/v3 target `Orders/CreateOrders`
(plural) with all mandatory fields per
https://help.linnworks.com/support/solutions/articles/7000013635 .

### Working wire format

`POST /api/Orders/CreateOrders` with JSON body:

```json
{ "orders": [ <order> ] }
```

i.e. an array under the `orders` key, even when sending a single
order. Standard Linnworks JSON request — **not** form-encoded
(despite what the help-article example suggests).

### Required fields on the order object

```json
{
  "Source":          "DIRECT",
  "SubSource":       "SQUARE_POS",
  "ReferenceNumber": "<unique per Source+SubSource>",
  "ExternalReferenceNumber": "<typically same as ReferenceNumber>",
  "ReceivedDate":    "<ISO 8601 datetime>",
  "DispatchBy":      "<ISO 8601 datetime, > now>",
  "LocationId":      "<stock location UUID>",
  "Currency":        "GBP",
  "OrderItems": [
    {
      "SKU":          "<linnworks SKU>",
      "ChannelSKU":   "<typically same as SKU>",
      "ItemTitle":    "<line description>",
      "ItemNumber":   "<typically same as SKU>",
      "Qty":          1,
      "PricePerUnit": 0.01,
      "Discount":     0,
      "LineDiscount": 0,
      "TaxRate":      0
    }
  ],
  "DeliveryAddress": {
    "FullName":     "...",
    "EmailAddress": "...",
    "PhoneNumber":  "...",
    "Address1":     "...",
    "Town":         "...",
    "PostCode":     "...",
    "Country":      "United Kingdom",
    "CountryCode":  "GB"
  },
  "BillingAddress": "<same shape as DeliveryAddress>"
}
```

The address must be named `DeliveryAddress` — NOT `ShippingAddress`.
That single rename was one of the silent 400 causes on v1.

### Response shape

A bare JSON array of pkOrderID strings, one per order in the request:

```json
["98c01c1a-cdfd-46f2-9bce-4c19d268bbe0"]
```

This is **not** wrapped in `{Orders: [...]}` or `{Data: [...]}` — it's
a top-level array of UUID strings. v2 had a parser bug that only
checked for dict-shaped responses with pk fields; v3's parser
self-tests against this exact prod shape so a regression breaks
loudly in CI.

### Dedup key

Linnworks deduplicates `Orders/CreateOrders` calls on the triple
`(Source, SubSource, ReferenceNumber)`. Re-submitting the same triple
returns the same `pkOrderID` — no error, no duplicate. **Important
for Phase 3 design**: the `ReferenceNumber` we use for Square→
Linnworks orders should derive deterministically from Square's order
ID so the order-pull cron is naturally idempotent.

### Cleanup endpoint

`POST /api/Orders/DeleteOrder` (singular) with body:

```json
{ "orderId": "<pkOrderID uuid>" }
```

Returns 200. The plural `Orders/DeleteOrders` and the `CancelOrder`
fallbacks were not needed.

### Stock locations on this tenant

Three stock locations confirmed (per the probe's
`Inventory/GetStockLocations` listing). One is locked in:

- **Default** — `StockLocationId = 00000000-0000-0000-0000-000000000000`,
  `IsFulfillmentCenter = false`. This is the location we use on
  Square→Linnworks orders.

The other two locations' names and UUIDs are visible in any probe-3
run log under the line `=== DISCOVERY: 3 stock location(s) on tenant
===`. Paste them in here when convenient — they're not on the
critical path for Phase 1–3 since stock-push and order-pull both
only touch the Default location, but it's worth recording.

---

## 4. Linnworks "mark as paid (no dispatch)" mechanism

**Probe**: `probes/probe_linnworks_mark_paid.py`
**Workflow**: `.github/workflows/probe-linnworks-mark-paid.yml`
**Status**: ⚠️ **IN PROGRESS** — every candidate endpoint returned
HTTP 404. The actual endpoint needs to be researched from
apidocs.linnworks.net before the next probe attempt.

### Endpoints ruled out (returned 404 — do NOT re-test next session)

The probe attempted six request shapes across four unique endpoint
paths. **All four paths returned HTTP 404 on this tenant** — the
endpoints don't exist (or are on the wrong cluster, but the cluster
URL is the one returned by `Auth/AuthorizeByApplication` so that's
unlikely):

- `Orders/SetPaymentStatus`  (3 body shape variants — camel, Pascal, request-wrapped)
- `Orders/AddOrderPayment`
- `Orders/SetOrderPayment`
- `Orders/PayOrder`

### Baseline payment fields on a fresh CreateOrders order

Captured via `Orders/GetOrdersById` immediately after creation:

| Field | Value |
|---|---|
| `GeneralInfo.Status`              | `0` |
| `TotalsInfo.PaymentMethodId`      | `"00000000-0000-0000-0000-000000000000"` |
| `IsParked`                        | `true` |

`Status = 0` likely means "open / received" (Linnworks doesn't
publish the enum but `1` typically means "processed" i.e. dispatched).
`PaymentMethodId` zeroed out is the "no payment recorded" state.

### `IsParked: true` — Phase 3 design implication

Orders created via `Orders/CreateOrders` with `Source = "DIRECT"`
land in a **parked** state. Parked orders sit outside Linnworks'
normal dispatch flow — they can't be processed or dispatched, and
Kevin won't see them in his usual "open orders" view until they're
unparked.

This is probably *why* the obvious mark-paid endpoints don't apply:
the order is parked, payment can't be recorded against it directly.
The Phase 3 order-pull flow may need to:

1. Create the order via `Orders/CreateOrders` (parked).
2. Unpark the order (endpoint TBD — research with mark-paid).
3. Mark as paid (endpoint TBD).
4. Leave open (do NOT dispatch — Kevin processes manually).

…but this is speculation until we find the right endpoints.

### Action for next session

Before any further probe attempts, search apidocs.linnworks.net for:

- the canonical mark-as-paid endpoint name (likely something we
  haven't tried yet — possibly under `Payments/`, `OrderPayments/`,
  or a `ProcessedOrders/` path even though our orders aren't
  processed)
- the unpark endpoint (search "park" / "unpark")
- whether mark-paid is a side-effect of unparking + setting a payment
  method ID, rather than a dedicated endpoint

Update this section with the candidates *before* writing more probe
attempts. The current probe layout (create test order → try paths →
verify via readback → cleanup in finally) is sound and re-runnable;
just plug new endpoint candidates into `_candidate_mark_paid_calls()`.

---

## 5. Supabase write patterns

**Probe**: `probes/probe_supabase_write_pattern.py`
**Workflow**: `.github/workflows/probe-supabase-write-pattern.yml`
**Status**: ✅ all four patterns confirmed working.

1. **Upsert with `on_conflict='sku'`** on `sq_sku_map` — second
   upsert with same SKU updates rather than duplicates. ✅
2. **Batch insert** of multiple rows into `sq_errors` in one call,
   with `jsonb` `context` round-tripping intact (nested dicts and
   arrays preserved). ✅
3. **Watermark read/write/overwrite round-trip** on `sq_watermarks` —
   `set_watermark()` → `get_watermark()` returns exactly what was
   written; overwriting the same key updates rather than duplicates. ✅
4. **Filtered query** on `sq_sync_runs` chaining `.eq("status", ...)`
   and `.gte("started_at", ...)` returns the expected rows. ✅

The probe cleans up after itself by deleting all rows tagged with the
`__probe_test__` markers — verified clean at end of run.

---

## How probes communicate findings

Every probe prints lines starting with `=== DISCOVERY: ===` to stdout.
After running a probe, search the GitHub Actions run log for that
prefix and copy the lines into the relevant section above.

Example log fragment:

```
=== DISCOVERY: ITEMS_READ scope OK on /v2/catalog/list ===
=== DISCOVERY: Orders/CreateOrders works with shape: JSON, {orders:[order]} ===
=== DISCOVERY: response shape: bare JSON array of pkOrderID strings, e.g. ["<uuid>"] ===
=== DISCOVERY: cleanup via Orders/DeleteOrder succeeded ===
```

That single grep is the entire workflow for moving findings from
"observed in CI" to "checked into the repo".
