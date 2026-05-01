# Linnworks API — Working Reference

A practical reference for building integrations against the Linnworks REST API. Written engineer-to-engineer, with the gotchas surfaced. Intended to be copied into any project that needs to talk to Linnworks; **stands alone**, not tied to any specific app.

Last validated: April 2026, against Linnworks' EU cluster. Their API surface drifts; treat field names as ground truth only after you've hit them with a real call.

---

## 1. Tenant context

This document was written against a Linnworks account hosted on the **EU cluster**. The base URL is:

```
https://eu-ext.linnworks.net
```

**Critical**: Linnworks runs multiple regional clusters (US, EU, etc.). Calling the wrong base URL **silently 404s** — you don't get a friendly "wrong region" error. Always start every session with the auth call (§2) and use the `Server` URL it returns. Don't hardcode `eu-ext.linnworks.net`.

The tenant has the typical single-warehouse setup:
- **Default** location — the real, operational warehouse.
- May also have FBA / 3PL placeholder locations that exist in Linnworks but carry zero stock — ignore unless told otherwise.

Currency mix on POs / supplier records:
- **GBP** — default for the tenant + most UK suppliers
- **JPY** — Hosco (Japan)
- **EUR** — Schaller (Germany)
- **USD** — Witweet (US)

If your integration touches money (line cost, shipping declared value, etc.), don't assume GBP — read the supplier record or the order header.

---

## 2. Authentication

Linnworks uses an OAuth-ish two-step flow. There's no refresh-token dance — you exchange long-lived install credentials for a short-lived session token.

### The flow

```
POST https://api.linnworks.net/api/Auth/AuthorizeByApplication
Content-Type: application/x-www-form-urlencoded

ApplicationId=<your app id>
ApplicationSecret=<your app secret>
Token=<the install token issued to this tenant>
```

Returns:

```json
{
  "Token": "<session token, ~32 chars>",
  "Server": "https://eu-ext.linnworks.net",
  ...other identity fields
}
```

### Three credentials, three names — easy to confuse

| Name in API | What it is | Lifetime |
|---|---|---|
| `ApplicationId` | Your developer-app id | Forever |
| `ApplicationSecret` | Your developer-app secret | Forever (rotate manually) |
| `Token` (in request) | The **install token** for this tenant — issued when the tenant installed your app | Forever (until the tenant uninstalls) |
| `Token` (in response) | The **session token** for subsequent API calls | Hours; re-auth on 401 |

### Using the session token

The session token goes in the `Authorization` header — **no `Bearer` prefix**, just the raw token:

```
Authorization: <session token>
```

Body content type for most endpoints is `application/json`. Notable exceptions:
- The auth call itself uses `application/x-www-form-urlencoded`.
- `Dashboards/ExecuteCustomPagedScript` uses `application/x-www-form-urlencoded` (see §6).

### Python skeleton

```python
import requests

AUTH_URL = "https://api.linnworks.net/api/Auth/AuthorizeByApplication"


def authorize(app_id: str, app_secret: str, install_token: str) -> tuple[str, str]:
    """Exchange install credentials for a session token + cluster URL.

    Returns (session_token, server_url). ALWAYS use the returned server,
    never a hardcoded one — the tenant's cluster is determined by the
    auth response.
    """
    resp = requests.post(
        AUTH_URL,
        data={
            "ApplicationId": app_id,
            "ApplicationSecret": app_secret,
            "Token": install_token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    return body["Token"], body["Server"]


def call(server: str, session_token: str, path: str, body: dict | None = None) -> dict:
    """Generic POST helper for JSON endpoints."""
    resp = requests.post(
        f"{server}/api/{path}",
        headers={"Authorization": session_token, "Content-Type": "application/json"},
        json=body or {},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()
```

Re-auth on 401 (session expired) and retry. Don't silently swallow — log it so you can spot if your install token has been revoked.

---

## 3. Stock & Inventory APIs

Most-used endpoints. All POST + JSON unless noted.

### `Stock/GetStockItems` — paginated SKU list

Light-weight list of items. Use when you just need `(StockItemId, ItemNumber, ItemTitle)`.

```json
{
  "keyword": "",
  "loadCompositeParents": false,
  "loadVariationParents": false,
  "entriesPerPage": 200,
  "pageNumber": 1,
  "dataRequirements": [],
  "searchTypes": []
}
```

`searchTypes`: `[0]` = SKU, `[1]` = Title, `[2]` = Barcode. Pass an empty array for "all".

### `Stock/GetStockItemsFull` — fully hydrated item

Heavy. Returns the same item shape with optional sub-objects controlled by `dataRequirements`:

| `dataRequirements` value | Includes |
|---|---|
| `1` | Stock levels (per location) |
| `2` | Pricing (sales channel prices) |
| `4` | Suppliers |
| `8` | Tracking |
| `16` | Images |
| ... | (see Linnworks docs) |

Body is the same shape as `GetStockItems`. Field-level traps to know:

- **`Code`**, not `SupplierCode`. Multiple supplier records, but the supplier's reference for the SKU is in `Code`. This trips up almost everyone.
- **`MinimumLevel` lives on `Stock/GetStockLevel`**, NOT on the inventory item. `GetStockItemsFull` does not return reorder points.
- **`BinRack` is on the per-location stock object**, not the item. Same call, different field.

### `Stock/GetStockLevel` — per-location stock for one item

```json
{ "stockItemId": "<uuid>" }
```

Returns an array, one row per stock location:

```json
[
  {
    "Location": { "StockLocationId": "<uuid>", "LocationName": "Default" },
    "StockLevel": 42,
    "MinimumLevel": 5,
    "Available": 40,
    "InOrders": 2,
    "Due": 100,
    "BinRack": "A1-3",
    ...
  }
]
```

Sum `StockLevel` across locations for total stock. The "Default" location is usually the only meaningful one.

### `Stock/GetStockLevel_Bulk` — batch version

Same as above but takes an array of `stockItemIds`. Use when you need stock for many items at once — much faster than N individual calls.

```json
{ "stockItemIds": ["<uuid1>", "<uuid2>", ...] }
```

Response groups levels by stock-item-id. Confirm exact shape on first call.

### `Stock/GetStockLocations` — list locations

No body, returns array of `{ StockLocationId, LocationName, IsFulfillmentCenter, ... }`.

### `Inventory/GetInventoryItemSuppliers` — supplier details for a SKU

If you didn't include `dataRequirements: [4]` on the items call. Returns the supplier objects with `Supplier` (name), `IsDefault`, `LeadTime`, `AverageLeadTime`, `SupplierMinOrderQty`, `SupplierPackSize`, `Code`, `SupplierBarcode`, `SupplierCost`.

**Don't trust `AverageLeadTime`** — it's computed from delivery history but goes stale and misleading. Use `LeadTime` (the supplier's stated lead time).

### `Stock/GetStockSold` — DEAD END for velocity

Despite the name, this is **not** a velocity / sales-history endpoint. It returns "items also bought" — SKUs that co-occurred on orders alongside the queried `stockItemId`. A market-basket correlation report, not a units-sold report.

For actual sales velocity, see §6.

### Field-name trap summary

| If you want… | Look at… | NOT… |
|---|---|---|
| Supplier's reference for this SKU | `Code` (on supplier object) | `SupplierCode` |
| Reorder point | `MinimumLevel` on `GetStockLevel` | inventory item |
| Bin location | `BinRack` on `GetStockLevel` | inventory item |
| Real lead time | `LeadTime` | `AverageLeadTime` |
| Sales velocity | `Dashboards/ExecuteCustomPagedScript` Script 47 | `Stock/GetStockSold` |

---

## 4. Order APIs

### Three identifiers per order — pick the right one

Linnworks orders have **three** identifiers, used in different contexts:

| Identifier | Type | Source | When to use |
|---|---|---|---|
| `pkOrderID` | UUID | Linnworks-internal | All write calls (`SetOrderShippingInfo`, etc.). Pass arrays of these to `GetOrdersById`. |
| `NumOrderId` | int (sequential) | Linnworks-assigned | Human-readable internal id. Useful for logs / Linnworks UI URLs. Don't use as a join key with other systems. |
| `ReferenceNum` (sometimes `ExternalReference`) | string | Sales channel — Shopify order #, Amazon order ID, eBay item #, etc. | **Join key when matching against any non-Linnworks system** (Easyship, Slack, your own DB). The channel and Linnworks both see this independently. |

Rule of thumb: if you're talking to Linnworks, use `pkOrderID`. If you're talking to anything else, find the order via `ReferenceNum`.

### `Orders/GetOpenOrders` — paginated open-order list

Open = not yet dispatched. Body shape (verify against your tenant):

```json
{
  "entriesPerPage": 200,
  "pageNumber": 1,
  "filters": { },
  "sorting": []
}
```

Returns `{ Data: [<orders>], TotalEntries, TotalPages, ... }`. Fields on each order include `pkOrderID`, `NumOrderId`, `GeneralInfo` (with `ReferenceNum`, source, status), `ShippingInfo`, `Items`, `CustomerInfo`, etc.

### `Orders/SearchOrders` — filtered search

For finding orders by criteria — e.g. by `ReferenceNum`. The exact body shape varies by version of the API; **probe it** before relying on it. A reasonable starting attempt:

```json
{
  "request": {
    "SearchTerm": "<reference number>",
    "SearchField": "ReferenceNum",
    "SearchSorting": { "SortField": "dReceivedDate", "SortDirection": "DESC" },
    "PageNumber": 1,
    "EntriesPerPage": 50
  }
}
```

If that 400s, try without the `request` wrapper, or check the Linnworks docs for the current schema. Use the §10 diagnostic pattern.

### `Orders/GetOrdersById` — full order by pkOrderID

```json
{ "pkOrderIds": ["<uuid1>", "<uuid2>"] }
```

Returns full hydrated orders — header + items + customer + shipping. Useful after `SearchOrders` gives you ids and you need the rest.

### `ProcessedOrders/SearchProcessedOrdersPaged` — DEAD END (so far)

Tried this for sales-velocity work; returned **400 on every body shape** we attempted (flat, request-wrapped, with/without `SearchSorting`, with/without `SearchFilters`). Either the endpoint signature changed, the tenant doesn't expose it, or the docs are stale. Didn't pursue because Script 47 (§6) covered the use case. Flagging here in case you find a working shape — please update this doc if so.

---

## 5. Purchase Order APIs

### Status lifecycle

```
PENDING  →  OPEN  →  PARTIAL  →  DELIVERED
(draft)    (sent)    (some in)   (complete)
```

- **`PENDING`** = drafted but not sent to the supplier. Treat as zero on-order.
- **`OPEN`** = sent, awaiting delivery.
- **`PARTIAL`** = some line items received, some pending.
- **`DELIVERED`** = closed.

For "what's actually on order", filter to **`OPEN` + `PARTIAL`** only. Don't include `PENDING` — drafts inflate your numbers.

### `PurchaseOrder/Search_PurchaseOrders2` — list POs by status

Note the underscores and the `2` suffix — older `Search_PurchaseOrders` (no 2) is deprecated.

```json
{
  "Status": "OPEN",
  "PageNumber": 1,
  "EntriesPerPage": 100
}
```

Returns headers only — no line items. Page until exhausted (`CurrentPageNumber >= TotalPages` or page returns < `EntriesPerPage`).

To cover both OPEN and PARTIAL, call twice and dedupe by `pkPurchaseID`.

### `PurchaseOrder/Get_PurchaseOrder` — full PO with line items

```json
{ "pkPurchaseID": "<uuid>" }
```

Returns header fields + a `PurchaseOrderItem` array of line items in **one call**. The header may be flat on the response root or wrapped in a `PurchaseOrderHeader` key — handle both. Likewise the items array key has been observed as `PurchaseOrderItem`, `Items`, or `Lines` depending on tenant / version.

Header fields you'll typically want:
- `pkPurchaseID` (UUID)
- `ExternalInvoiceNumber` (the supplier's invoice/PO #, human-readable)
- `Status` (`OPEN`, `PARTIAL`, etc.)
- `fkSupplierId` (UUID — Linnworks-internal supplier id; we have **no API to map this back to a supplier name** without a separate `Inventory/GetSupplierList` call)
- `DateOfPurchase`
- `QuotedDeliveryDate`

Line item fields:
- `pkPurchaseItemId` (UUID — the line)
- `fkStockItemId` (UUID — links to the stock item)
- `SKU` (string — for human-readable joins)
- `Quantity` (ordered)
- `Delivered` (received so far)
- `Cost` (per-unit cost on this PO)

`qty_remaining = Quantity − Delivered`. Skip lines where this is `<= 0` if you only care about what's still inbound.

### Gotcha: duplicate (PO, SKU) pairs

Linnworks **allows the same SKU on multiple lines of a single PO** (e.g. someone added it twice as separate lines). If you have a database table keyed on `(pk_purchase_id, sku)` and try to upsert, you'll hit a constraint error.

**Aggregate before upserting**:
- `qty_ordered` → SUM
- `qty_delivered` → SUM
- `unit_cost` → first non-null (or weighted avg if you care)
- `fk_stock_item_id` → first non-null
- All header fields → identical across lines, just take the first

---

## 6. Sales / Velocity APIs

The right path is **`Dashboards/ExecuteCustomPagedScript`** — Linnworks' built-in "Query Data" script runner. Treat it like a parametrised stored procedure.

### Endpoint shape

```
POST /api/Dashboards/ExecuteCustomPagedScript
Content-Type: application/x-www-form-urlencoded
```

Yes, **form-urlencoded, not JSON**. This trips people up. Body fields:

| Field | Type | Notes |
|---|---|---|
| `scriptId` | string | Numeric script id (see catalogue below) |
| `entriesPerPage` | string | e.g. `"500"` |
| `pageNumber` | string | 1-indexed |
| `parameters` | string | JSON-encoded **string** of an array of `{Type, Name, Value}` objects |

The `parameters` field is recursively encoded: it's a string in form-encoding terms, but the string value is JSON. So in Python:

```python
import json, requests

form_body = {
    "scriptId": "47",
    "entriesPerPage": "500",
    "pageNumber": "1",
    "parameters": json.dumps([
        {"Type": "Date", "Name": "startDate", "Value": "2026-01-30"},
        {"Type": "Date", "Name": "endDate",   "Value": "2026-04-30"},
    ]),
}

resp = requests.post(
    f"{server}/api/Dashboards/ExecuteCustomPagedScript",
    data=form_body,                                    # <-- data=, not json=
    headers={"Authorization": session_token},
    timeout=120,
)
```

**Param names are case-sensitive and per-script.** The error message when wrong is helpfully explicit:

```
"Expected parameter 'endDate' to exist."
```

— which tells you both that the param is required and exactly what name to use.

### Script catalogue (this tenant)

| Script | Name | Status | Params |
|---|---|---|---|
| **47** | Sold Granular - Between Dates | ✅ works | `startDate`, `endDate` (camelCase, lowercase 'e') |
| 1 | Sold Stock Between Dates by Location | ❌ not present on this tenant ("Script does not exist") |
| 94 | Composite Parent Sales History | ❓ wants `Daterange` typed as `Int32` (probably a numeric ID for a predefined range, not a from/to pair); didn't pursue |

Script availability varies per tenant — what's listed in Linnworks' public help article isn't a guarantee yours has the same set. Always probe.

Reference for the full catalogue: <https://help.linnworks.com/support/solutions/articles/7000018696>.

### Script 47 response shape

```json
[
  {
    "SKU": "EP-0055-000",
    "Item Title": "Switchcraft 1/4\" Mono Output Jack Socket",
    "Item Purchase Price": 1.73,
    "Total Qty Sold": 124,
    "Avg Sold Price Ex VAT": 4.16,
    "TotalRows": 1842
  },
  ...
]
```

Note the **column names contain spaces** — `"Total Qty Sold"`, `"Avg Sold Price Ex VAT"`, `"Item Purchase Price"`, `"Item Title"`. Quote them carefully in any join logic.

`TotalRows` is the same value on every row (the result-set total) — useful for pagination.

### Critical attribution caveat

The `SKU` field on each row is **whatever was on the order line at sale time**. If you sell composite/kit products, this can be either:
- the **kit SKU** (the kit shipped as a single unit), or
- a **component SKU** (the kit was exploded into its components at sale time)

Behaviour depends on the channel and the kit configuration — Linnworks doesn't normalise this for you. If you need full per-component depletion, you must:
1. Query Script 47, get one row per SKU.
2. Cross-join against your BOM (kit → components) in your own logic.
3. For each kit SKU in the response, attribute its `Total Qty Sold` to each component via `qty_per_kit × kit_qty_sold`.
4. Sum direct + kit-attributed per component.

There's no risk of double-counting because a given sale only logs one of the two — kits that ship intact appear under the kit SKU, kits that explode appear under component SKUs. Sum across both bands cleanly.

### Pagination

Page until `len(rows) < entriesPerPage` OR you've seen `TotalRows`. Response doesn't include explicit `TotalPages` / `CurrentPageNumber` fields on the script-runner endpoint — different from the rest of the API.

---

## 7. Channel reference / external order matching

**Use `ReferenceNum`** when matching Linnworks orders to anything else.

The flow when an external system has a tracking number / shipping label / customer note that needs to land on a Linnworks order:

```
External system            Linnworks
────────────────           ─────────
shipment.order_reference   Orders/SearchOrders ?SearchField=ReferenceNum
       │                            │
       └────────── match ───────────┘
                    │
                    ↓
              pkOrderID (uuid)
                    │
                    ↓
           Orders/SetOrderShippingInfo
           (or whichever write you need)
```

### Why `ReferenceNum`, not `pkOrderID` or `NumOrderId`?

- `pkOrderID` is **internal to Linnworks** — third-party systems don't see it. Easyship / Shopify / your CRM don't store it.
- `NumOrderId` is **sequential and Linnworks-assigned** — also internal, also not present in third-party systems.
- `ReferenceNum` is **the channel's order number** (Shopify order #, Amazon order ID, eBay transaction id). Both Linnworks and the third-party system see it because both ingested it from the same channel.

### Edge cases when matching

- **Multiple Linnworks orders with the same `ReferenceNum`**: rare but possible (refund + reshipment, manual duplicates). Handle by picking the most recent open order, or flagging the ambiguity.
- **No match**: order may have been cancelled, deleted, or never made it to Linnworks. Log it, don't silently ignore.
- **Channel-prefix variations**: some channels prepend a marketplace code (e.g. `AMZ-203-1234567`), some don't. Confirm whether the third-party system stores the prefixed or unprefixed form, and align Linnworks' search query accordingly.

---

## 8. Writing tracking numbers

This is the integration-target use case (e.g. Easyship → Linnworks). **The exact endpoints below are best-known candidates — probe them with the §10 diagnostic pattern before committing to production code.**

### Most likely path

```
POST /api/Orders/SetOrderShippingInfo
{
  "orderId": "<pkOrderID uuid>",
  "info": {
    "Vendor":            "<carrier name, e.g. 'Royal Mail'>",
    "PostalServiceName": "<service name, e.g. 'Tracked 24'>",
    "PostalServiceId":   "<linnworks postal service uuid (optional?)>",
    "TrackingNumber":    "<tracking string>",
    "Weight":            <kg as number>,
    "TotalWeight":       <kg as number>
  }
}
```

Returns the updated order. Probe whether `PostalServiceId` is required vs `PostalServiceName` is enough. If your write fails with "Postal service not found", you may need to first call `PostalServices/GetPostalServices` to get a UUID for the service the carrier maps to.

### Adjacent endpoints worth knowing

- `Orders/UpdateOrderShippingInfo` — possible synonym for the above; some Linnworks docs mention it. Probe.
- `Orders/MarkOrderAsDispatched` — moves the order from "open" to "processed". This is what triggers Linnworks' **dispatch propagation** to the source channel (Shopify gets fulfilled, Amazon gets a tracking notification, etc.). Body: `orderId` + `dispatched_date`.
- `PostalServices/GetPostalServices` — list of configured carriers + service ids on the tenant. Use this to look up the right `PostalServiceId` to write.

### The architectural win

Writing tracking + dispatch to **Linnworks** (not directly to each channel) is the high-leverage move. Linnworks then propagates the dispatch event to whichever channel the order originated from (Shopify, Amazon, eBay, your own site) using its built-in channel integrations. You get fulfilment reporting on every channel without writing per-channel code.

If your integration just stops at "tracking is on the Linnworks order but the order is still 'open'", you've won half — Linnworks will surface the tracking in its UI. To actually mark as shipped on the channel, follow up with `MarkOrderAsDispatched`.

### Idempotency

If your job runs every N minutes and pulls all "shipped today" shipments from the upstream system, you'll re-process the same shipment many times. Decide:

- **Skip if Linnworks already has a tracking number on the order** — read first via `Orders/GetOrdersById`, check `ShippingInfo.TrackingNumber`, only write if empty (or if it differs and you want to overwrite).
- **Or**: log every successful write to your own DB and skip shipments you've already processed.

The audit-log approach is more robust to manual edits in Linnworks (someone adds a tracking number by hand; your job won't clobber it).

---

## 9. Rate limits and reliability

### Per-method limits

Linnworks rate-limits per endpoint. Documented limits (verify against your tenant's plan):
- Most read endpoints: ~150–250/min
- Some heavier endpoints (e.g. `Get_PurchaseOrder`): 250/min
- Bulk endpoints have lower limits

You'll hit limits during heavy diagnostic sessions. The response on rate-limit is typically **HTTP 429** with `Retry-After` in the headers — back off and retry.

### Status code interpretation

| HTTP | Most likely cause |
|---|---|
| `200` | Success |
| `400` | Body shape wrong. The error message is **infuriatingly opaque** ("The request is invalid") most of the time — **try multiple body shapes** (flat / request-wrapped / SearchParameters-wrapped) before assuming the endpoint is broken. Sometimes the error text *does* name a missing param explicitly — read it carefully. |
| `401` | Token expired (re-auth), or wrong cluster (use the `Server` from auth response, not a hardcoded URL), or genuine auth failure. |
| `403` | Permission denied. Your app lacks scope for this endpoint. |
| `404` | Endpoint doesn't exist on this version, or **wrong cluster** (silent 404 is the classic symptom). |
| `429` | Rate-limited. Back off. |
| `500` | Linnworks-side error. Rare but happens. Retry with backoff. |

### Recommended retry policy

Exponential backoff on 429 / 500 / 502 / 503, max ~5 retries. Don't retry 400 / 401 / 403 / 404 — fix the request, not the timing.

---

## 10. Diagnostic-first development pattern

Linnworks' API surface is wide, inconsistently documented, and varies subtly between tenants. **Probe before you write production code.** This pattern has saved us hours on every new endpoint:

### Pattern

1. Write a **diagnostic-only script** that hits the candidate endpoint with multiple body / parameter shapes.
2. Log: HTTP status, response body (truncated to ~500 chars on error), and the field-name summary of any successful response.
3. Run it via a **manual-trigger CI job** (GitHub Action with `workflow_dispatch`), not from a laptop — so credentials stay in CI secrets and never touch local env files.
4. **Read the error messages.** Linnworks' 400s often tell you the exact param name expected, the exact type expected, or that the script doesn't exist on this tenant.
5. Once a working shape is found, **lock it in via a commit** and write the production ingestion against that shape. Keep the diagnostic file in the repo for the next debugging session.

### Python skeleton

```python
import json, os, sys, requests

AUTH_URL = "https://api.linnworks.net/api/Auth/AuthorizeByApplication"


def authorize(app_id, app_secret, install_token):
    resp = requests.post(AUTH_URL, data={
        "ApplicationId": app_id,
        "ApplicationSecret": app_secret,
        "Token": install_token,
    }, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    return body["Token"], body["Server"]


def probe(server, token, path, *, method="POST", body=None, params=None,
          form=None, label=""):
    url = f"{server}/api/{path}"
    print(f"\n--- {label} ---")
    print(f"{method} {url}")
    if body is not None: print(f"JSON body: {json.dumps(body)[:300]}")
    if form is not None: print(f"Form body: {form}")
    if params is not None: print(f"Params:    {params}")

    headers = {"Authorization": token}
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, params=params, timeout=60)
        elif form is not None:
            resp = requests.post(url, headers=headers, data=form, timeout=60)
        else:
            headers["Content-Type"] = "application/json"
            resp = requests.post(url, headers=headers, json=body, timeout=60)
    except Exception as e:
        print(f"REQUEST FAILED: {type(e).__name__}: {e}")
        return None

    print(f"HTTP {resp.status_code}")
    if not resp.ok:
        print(f"Error body: {resp.text[:500]}")
        return None
    try:
        return resp.json()
    except ValueError:
        print(f"Non-JSON: {resp.text[:500]}")
        return None


def main():
    token, server = authorize(
        os.environ["LINNWORKS_APP_ID"],
        os.environ["LINNWORKS_APP_SECRET"],
        os.environ["LINNWORKS_TOKEN"],
    )
    print(f"Authenticated. Server: {server}")

    # Try multiple body shapes for the same endpoint, log each.
    attempts = [
        ("flat",                {"key": "value"}),
        ("request wrapper",     {"request": {"key": "value"}}),
        ("SearchParameters",    {"SearchParameters": {"key": "value"}}),
    ]
    for label, body in attempts:
        result = probe(server, token, "Some/Endpoint", body=body, label=label)
        if result is not None:
            print("WORKED — locked-in shape:", label)
            print(json.dumps(result, indent=2, default=str)[:2000])
            break


if __name__ == "__main__":
    sys.exit(main() or 0)
```

Run via a manual-trigger workflow with the three Linnworks env vars wired from secrets. Output goes to the workflow log; copy/paste relevant bits into the next iteration of the diagnostic.

---

## 11. Suggested architecture for the Easyship → Linnworks tracking bridge

The use case: Easyship is the system of record for shipments. Each shipment carries a tracking number once the label is generated. We want that tracking number to land on the corresponding Linnworks order so Linnworks can propagate dispatch back to whichever channel sourced the order.

### Flow

```
┌─────────────────────────────────────────────┐
│ GitHub Action (cron, every 15-60 min)       │
│   or webhook from Easyship if available     │
└────────────────────┬────────────────────────┘
                     ↓
            Pull shipments from Easyship
            since last successful run
                     ↓
        For each shipment with tracking_number:
                     ↓
            Lookup Linnworks order by
            ReferenceNum = shipment.order_reference
            (Orders/SearchOrders)
                     ↓
        ┌────────────┴────────────┐
        ↓                         ↓
    Match found             No match — log + skip
        ↓
    Read order's existing ShippingInfo
    (Orders/GetOrdersById)
        ↓
    ┌───────────────┴────────────────┐
    ↓                                ↓
Tracking already set            Tracking empty
(skip / log noop)               (write new)
                                    ↓
                              Orders/SetOrderShippingInfo
                                    ↓
                          (optional) MarkOrderAsDispatched
                                    ↓
                         Audit log to Supabase / your DB
                                    ↓
                            Slack notification
                            (re-uses existing flow)
```

### Components

1. **Easyship pull**: incremental, watermark-based. Track `last_processed_shipment_at` (or `last_seen_shipment_id`) in your DB; on each run, fetch shipments since that watermark. Easyship's API supports `created_after` filters.

2. **Order lookup**: `Orders/SearchOrders` filtered by `ReferenceNum`. Cache results within a run (multiple shipments for the same order would otherwise hit the API redundantly).

3. **Existing-tracking guard**: read the order's current `ShippingInfo.TrackingNumber` first. Skip the write if already set, unless your job is configured to overwrite (rare).

4. **Write call**: `Orders/SetOrderShippingInfo` with `pkOrderID`, carrier info, tracking number. Probe shape first (§10).

5. **Dispatch (optional)**: `Orders/MarkOrderAsDispatched` after the tracking write to actually close the order in Linnworks and trigger channel propagation. Decide whether your integration owns the dispatch event or whether someone else (warehouse staff) does it manually.

6. **Audit log**: every successful write goes to a row in your DB with `easyship_shipment_id`, `linnworks_order_id`, `tracking_number`, `written_at`, `success`, `error_text`. Two reasons:
   - **Idempotency**: skip shipments you've already processed.
   - **Debugging**: when something doesn't show up on the channel, you have a single timeline to check.

7. **Slack flow stays as-is**: the existing notification pipeline (whatever it is) can either keep reading from Easyship directly, or read from your audit log to confirm "tracking written + dispatched" before notifying.

### Failure modes to think through up front

| Scenario | What goes wrong | Mitigation |
|---|---|---|
| Channel reference not found in Linnworks | Order was cancelled, deleted, or never imported | Log + skip. Optionally alert if rate of misses is high |
| Order already dispatched manually | Linnworks rejects the write, or you'd overwrite a manual tracking number | Read first, skip if `TrackingNumber` already set |
| Same shipment processed twice | Duplicate write attempts | Audit-log idempotency check before processing |
| Easyship returns multiple shipments for one order | Multi-package order; channel only takes one tracking number | Pick the first shipment with a tracking number; or join multi-trackings into a delimited string in `TrackingNumber` (depends on what the channel can handle) |
| Carrier name doesn't match a Linnworks postal service | Write succeeds but channel doesn't get carrier info | Pre-build a carrier-name → `PostalServiceId` mapping table; cache via `PostalServices/GetPostalServices` |
| Linnworks token expired mid-run | All writes 401 | Wrap calls in re-auth-on-401 retry. The session token is short-lived |
| Easyship API rate limit | Pulls fail | Backoff + watermark stays put; next run picks up from where you left off |
| Linnworks rate limit (429 on writes) | Some writes fail mid-run | Backoff + retry. Don't advance the audit log until the write succeeds |
| Network flap mid-write | Ambiguous state — did the write land? | After a recoverable error, re-read `Orders/GetOrdersById` and check current state before deciding to retry the write |

### Where to host

Same pattern as any other batch integration:
- **GitHub Actions cron** for scheduling. Fine for sub-hourly cadences.
- **Cloudflare Pages Function** if you need a sync HTTP endpoint (e.g. webhook receiver from Easyship). Then a function that does the Linnworks call inline + writes to the audit log.
- **Supabase** (or any Postgres) for the audit log — same auth pattern most projects already have.

Don't build a long-running daemon for this — it's batch by nature.

---

## Appendix A — Common header / form patterns at a glance

```python
# Auth (form-urlencoded, response gives session token + cluster URL)
requests.post(AUTH_URL, data={"ApplicationId": ..., "ApplicationSecret": ..., "Token": ...})

# Standard JSON call
requests.post(
    f"{server}/api/Some/Endpoint",
    headers={"Authorization": token, "Content-Type": "application/json"},
    json={"key": "value"},
)

# GET with query params (rare)
requests.get(
    f"{server}/api/Stock/GetStockSold",
    headers={"Authorization": token},
    params={"stockItemId": "<uuid>"},
)

# Form-urlencoded (only Dashboards/ExecuteCustomPagedScript and the auth call)
requests.post(
    f"{server}/api/Dashboards/ExecuteCustomPagedScript",
    headers={"Authorization": token},
    data={
        "scriptId": "47",
        "entriesPerPage": "500",
        "pageNumber": "1",
        "parameters": json.dumps([...]),  # JSON-string of array
    },
)
```

---

## Appendix B — Endpoint quick reference

| Endpoint | Method | Body type | Purpose |
|---|---|---|---|
| `Auth/AuthorizeByApplication` | POST | form | Get session token + cluster URL |
| `Stock/GetStockItems` | POST | JSON | Light item list (paginated) |
| `Stock/GetStockItemsFull` | POST | JSON | Hydrated items (heavy) |
| `Stock/GetStockLevel` | POST | JSON | Per-location stock for one item |
| `Stock/GetStockLevel_Bulk` | POST | JSON | Per-location stock for many items |
| `Stock/GetStockLocations` | POST | JSON | List warehouse locations |
| `Stock/GetStockSold` | GET | params | "Items also bought" — NOT velocity |
| `Inventory/GetInventoryItemSuppliers` | POST | JSON | Suppliers per SKU |
| `Orders/GetOpenOrders` | POST | JSON | Paginated open orders |
| `Orders/SearchOrders` | POST | JSON | Filtered search (use for ReferenceNum lookups) |
| `Orders/GetOrdersById` | POST | JSON | Full hydrated orders by pkOrderID array |
| `Orders/SetOrderShippingInfo` | POST | JSON | Write carrier + tracking number |
| `Orders/MarkOrderAsDispatched` | POST | JSON | Close order + propagate to channel |
| `PostalServices/GetPostalServices` | POST | JSON | Configured carriers + ids |
| `ProcessedOrders/SearchProcessedOrdersPaged` | POST | JSON | DEAD END — body shape unknown |
| `PurchaseOrder/Search_PurchaseOrders2` | POST | JSON | List POs by status |
| `PurchaseOrder/Get_PurchaseOrder` | POST | JSON | Full PO with line items |
| `Dashboards/ExecuteCustomPagedScript` | POST | form | Run a Query Data script (e.g. Script 47 for sales) |

---

## Appendix C — Things to update in this doc

This file is a working reference; keep it honest. When you discover a new gotcha or confirm an unverified shape, update the relevant section. Specifically: §4 `Orders/SearchOrders` body shape, §8 `Orders/SetOrderShippingInfo` body, §6 the script catalogue per-tenant.
