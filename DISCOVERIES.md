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

**Phase 0b status**: ✅ COMPLETE 2026-05-02 — all 4 probes green
(Square scopes, Supabase write patterns, Linnworks CreateOrders,
Linnworks mark-paid).

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

**Probe**: `probes/probe_linnworks_mark_paid.py` (v5)
**Workflow**: `.github/workflows/probe-linnworks-mark-paid.yml`
**Status**: ✅ **CONFIRMED 2026-05-02** — locked-in two-step recipe.

The full mechanism is **two sequential calls**, both
`application/x-www-form-urlencoded` (NOT JSON). v1–v4 each got part
of this wrong; the working endpoints + body shapes were captured by
opening Linnworks dashboard DevTools and watching the actual
requests the UI fires.

### Step 1 — Unpark via `Orders/ChangeOrderTag`

```
POST /api/Orders/ChangeOrderTag
Content-Type: application/x-www-form-urlencoded
Authorization: <session token>

orderIds=%5B%22<uuid>%22%5D
```

The form value for `orderIds` is a **JSON-encoded array as a
string** — the literal characters `["<uuid>"]` (brackets and quotes
are part of the value), then URL-encoded by the HTTP client. No
other parameters; the endpoint name itself implies the unpark
action.

In Python:
```python
form = {"orderIds": json.dumps([pk_order_id])}
```

Response is a bare JSON array containing the `pkOrderID`.

### Step 2 — Mark paid via `Orders/ChangeStatus`

```
POST /api/Orders/ChangeStatus
Content-Type: application/x-www-form-urlencoded
Authorization: <session token>

orderIds=%5B%22<uuid>%22%5D&status=1
```

Same JSON-string-of-array form-field convention. Status enum:
**`0` = Unpaid, `1` = Paid** (confirmed via UI DevTools capture).
Response is the same bare array shape as step 1.

In Python:
```python
form = {"orderIds": json.dumps([pk_order_id]), "status": "1"}
```

### CRITICAL — ordering matters

`ChangeOrderTag` **MUST** run before `ChangeStatus`. Parked orders
silently no-op `ChangeStatus`: the call returns HTTP 200, but
`GeneralInfo.Status` stays at `0`. This is a no-error,
no-error-message failure mode and was the v3 false-success that
took several rounds to diagnose. **Production code must always
unpark first.**

### Verified post-call state

After both steps:

| Field | Value |
|---|---|
| `GeneralInfo.Status`              | `1`   (Paid) |
| `GeneralInfo.IsParked`            | `false` |
| `Processed`                       | `false` |
| `GeneralInfo.Processed`           | `false` |

The order shows as **Paid** in Linnworks' Open Orders view, **not
dispatched** — exactly what Phase 3 needs (Kevin processes
dispatch manually).

### Bonus observation — `PaidDateTime`

Linnworks automatically adds a `PaidDateTime` field to the order
when `Status` flips to `1`. We don't set it — the platform stamps
it server-side. Useful for audit logging in Phase 3.

### Baseline payment fields on a fresh CreateOrders order

Captured via `Orders/GetOrdersById` immediately after creation,
before either step runs:

| Field | Value |
|---|---|
| `GeneralInfo.Status`              | `0` (Unpaid per the enum) |
| `GeneralInfo.IsParked`            | `true` |
| `TotalsInfo.PaymentMethodId`      | `"00000000-0000-0000-0000-000000000000"` |
| `Processed`                       | `false` |

### Endpoints ruled out — DO NOT re-test

These returned HTTP 404 on this tenant during v1/v3 attempts. They
either don't exist or aren't routed on the EU cluster. Future
probes should not waste time on them:

- `Orders/SetPaymentStatus` (3 body shape variants attempted)
- `Orders/AddOrderPayment`
- `Orders/SetOrderPayment`
- `Orders/PayOrder`
- `Orders/SetOrderParkedStatus` (the obvious-sounding unpark
  endpoint — doesn't exist; the real one is `ChangeOrderTag`)

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
