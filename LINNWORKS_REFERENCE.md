# Linnworks API — Working Reference

A practical, vendor-neutral reference for building integrations against the Linnworks REST API. Written engineer-to-engineer, with the gotchas that aren't in the official docs surfaced. Intended to be copied into any project that talks to Linnworks; **stands alone**, not tied to any specific app.

Last validated: May 2026, against Linnworks' EU cluster.

The Linnworks API surface drifts and varies by tenant. Treat field names and body shapes as ground truth only after you've hit them with a real call against your tenant. Probe before you write production code (see §15).

---

## Table of contents

1. [Tenant & cluster context](#1-tenant--cluster-context)
2. [Authentication](#2-authentication)
3. [Rate limits & status codes](#3-rate-limits--status-codes)
4. [Endpoint catalogue at a glance](#4-endpoint-catalogue-at-a-glance)
5. [Stock & Inventory APIs](#5-stock--inventory-apis)
6. [Order APIs (read)](#6-order-apis-read)
7. [Order creation — deep dive](#7-order-creation--deep-dive)
8. [Order state transitions — the 3-step recipe](#8-order-state-transitions--the-3-step-recipe)
9. [Stock processing flow](#9-stock-processing-flow)
10. [Channel naming: Source / SubSource / ReferenceNumber](#10-channel-naming-source--subsource--referencenumber)
11. [VAT & tax handling](#11-vat--tax-handling)
12. [Sales / velocity APIs](#12-sales--velocity-apis)
13. [Writing tracking & dispatch](#13-writing-tracking--dispatch)
14. [Recommended integration architecture](#14-recommended-integration-architecture)
15. [Diagnostic-first development pattern](#15-diagnostic-first-development-pattern)
16. [Common gotchas (catch-all)](#16-common-gotchas-catch-all)
17. [Endpoints tried and ruled out](#17-endpoints-tried-and-ruled-out)
18. [Appendix A — Common request patterns](#appendix-a--common-request-patterns)
19. [Appendix B — Documentation links](#appendix-b--documentation-links)

---

## 1. Tenant & cluster context

### Multiple regional clusters

Linnworks runs multiple regional clusters (US, EU, etc.). Each tenant lives on exactly one. The cluster URL is **returned by the auth response** — do not hardcode.

Calling the wrong cluster returns **silent 404s**. There is no friendly "wrong region" message. The first symptom is "the endpoint doesn't exist" when the endpoint clearly does exist on the docs site. The fix is always: re-run auth, read `Server` from the response, use that base URL for every call in the session.

Common cluster URLs (illustrative — your tenant tells you which):

```
https://eu-ext.linnworks.net   (EU cluster, observed)
https://us-ext.linnworks.net   (US cluster, illustrative)
https://api.linnworks.net      (auth endpoint only — NOT for data calls)
```

### Stock locations

Most tenants have:
- A **Default** location with `StockLocationId = 00000000-0000-0000-0000-000000000000` (the all-zeros UUID). This is the Linnworks-internal "no specific location" / primary location.
- Optionally additional named locations (FBA, 3PL, etc.). Discover these via `Stock/GetStockLocations`.

Most simple integrations only care about the Default location. Don't paper over multi-location tenants by summing — read each location's stock separately if it matters.

### Currencies

Tenants can have orders, suppliers, and POs in mixed currencies. Read the currency field on each record rather than assuming the tenant's primary currency.

---

## 2. Authentication

Linnworks uses an OAuth-ish two-step flow. There is **no refresh-token dance** — you exchange long-lived install credentials for a short-lived session token, and re-auth when the session token expires.

### The three credentials

| Name | What it is | Lifetime | Where it comes from |
|---|---|---|---|
| `ApplicationId` | Your developer-app id | Forever | Linnworks Developer dashboard → your app |
| `ApplicationSecret` | Your developer-app secret | Forever (rotate manually) | Linnworks Developer dashboard → your app |
| `Token` (install token) | The per-tenant install grant — issued when the tenant installs your app | Forever (until tenant uninstalls) | Tenant clicks "install" on your app's listing → token shown once |

These are easy to confuse because the auth response also contains a field named `Token` (the **session** token). Treat the install token as a long-lived secret stored in your secret manager; treat the session token as ephemeral and never persisted to disk.

### The auth call

```
POST https://api.linnworks.net/api/Auth/AuthorizeByApplication
Content-Type: application/x-www-form-urlencoded

ApplicationId=<your_app_id>
ApplicationSecret=<your_app_secret>
Token=<install_token>
```

Response (JSON):

```json
{
  "Token":  "<session_token, ~32 chars>",
  "Server": "https://eu-ext.linnworks.net",
  "Id":     "<tenant_id>",
  "...":    "other identity fields"
}
```

Two fields you must capture:

- **`Token`** — the session token. Goes in `Authorization` header on every subsequent call.
- **`Server`** — the cluster URL for this tenant. Use this as the base URL for every subsequent call. **Never** hardcode the cluster URL — the same code may run against tenants on different clusters.

### Using the session token

```
Authorization: <session_token>
```

**No `Bearer` prefix** — just the raw token. The Linnworks API rejects `Bearer <token>` with a 401.

Body content type defaults to `application/json` for most endpoints. Two notable exceptions (see relevant sections):

- The auth call itself uses `application/x-www-form-urlencoded`.
- `Dashboards/ExecuteCustomPagedScript` uses `application/x-www-form-urlencoded` (see §12).
- `Orders/ChangeOrderTag` and `Orders/ChangeStatus` use `application/x-www-form-urlencoded` (see §8).

### Session lifetime

The session token is good for roughly **30 minutes of idle time**. Active use extends the lifetime; long-lived processes that idle and then resume will hit a 401 mid-run.

### Mid-run expiry: re-auth and retry

Standard pattern: cache `(session_token, server)` at the start of a run. On any call returning 401:

1. Clear the cached `(token, server)`.
2. Re-run the auth call to get a fresh session.
3. Retry the original call **once**.
4. If the retry also returns 401, fail loud — the install token has likely been revoked, or the `ApplicationSecret` is wrong.

### Working Python client skeleton

```python
import time
import requests

AUTH_URL = "https://api.linnworks.net/api/Auth/AuthorizeByApplication"
RATE_LIMIT_SLEEP = 1.1   # see §3

_session_token: str | None = None
_server: str | None = None


def _authenticate() -> tuple[str, str]:
    """Exchange install credentials for a session token + cluster URL."""
    resp = requests.post(
        AUTH_URL,
        data={
            "ApplicationId":     APP_ID,
            "ApplicationSecret": APP_SECRET,
            "Token":             INSTALL_TOKEN,
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    return body["Token"], body["Server"]


def _ensure_auth() -> tuple[str, str]:
    global _session_token, _server
    if _session_token is None or _server is None:
        _session_token, _server = _authenticate()
    return _session_token, _server


def call(path: str, *, json_body=None, form_body=None, timeout=60) -> object:
    """Authenticated POST. Re-auths once on 401 then retries."""
    global _session_token, _server
    time.sleep(RATE_LIMIT_SLEEP)

    token, server = _ensure_auth()
    headers = {"Authorization": token}
    url = f"{server}/api/{path}"

    if form_body is not None:
        resp = requests.post(url, headers=headers, data=form_body, timeout=timeout)
    else:
        headers["Content-Type"] = "application/json"
        resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)

    if resp.status_code == 401:
        _session_token = None
        _server = None
        token, server = _ensure_auth()
        headers["Authorization"] = token
        url = f"{server}/api/{path}"
        if form_body is not None:
            resp = requests.post(url, headers=headers, data=form_body, timeout=timeout)
        else:
            resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)
        if resp.status_code == 401:
            raise RuntimeError(
                "Linnworks auth failed twice in a row — check ApplicationSecret "
                "or whether the install token has been revoked."
            )

    resp.raise_for_status()
    return resp.json() if resp.content else None
```

---

## 3. Rate limits & status codes

### Rate limits

Linnworks rate-limits per endpoint per tenant. Documented limits vary; safe defaults for most read endpoints are around **150–250 requests/minute**, with bulk endpoints typically lower. Practical guidance:

- **Sleep ~1.1s between calls** during heavy reads (catalog walks, order backfills). This is well under any per-minute cap and rarely triggers a 429.
- For bulk writes (catalog upsert, batch inventory change), use the **batch endpoints** rather than N individual calls. The batch endpoints have their own limits but throughput is much higher.
- Probes can hit limits during heavy diagnostic sessions — write probes that don't fan out widely.

### 429 — rate-limited

Response includes a `Retry-After` header (in seconds) when present. Default policy:

- Back off by `Retry-After` if present, otherwise start at 2s and double.
- Cap retries at ~5 attempts.
- Do not retry inside a tight loop — the original sleep was the problem.

### Status code interpretation

| HTTP | Most likely cause | Retry? |
|---|---|---|
| `200` | Success. Response body may still indicate failure — read it. | — |
| `400` | Body shape wrong. Error message is often "The request is invalid" with no detail; sometimes names the missing param explicitly. **Try multiple body shapes** before assuming the endpoint is broken. See §15 (diagnostic-first). | No — fix the request. |
| `401` | Session token expired (re-auth + retry once), wrong cluster (use `Server` from auth response), or genuine auth failure. | Yes, once after re-auth. |
| `403` | Permission denied. Your developer app lacks scope for this endpoint. | No. |
| `404` | Endpoint doesn't exist on this version OR (very common) **wrong cluster**. The 404 looks identical in both cases. | No — fix the URL. |
| `429` | Rate-limited. Honour `Retry-After`. | Yes, with backoff. |
| `500/502/503` | Linnworks-side error. Rare but happens. | Yes, with backoff. |

### Recommended retry policy

Exponential backoff on 429 / 500 / 502 / 503 with max ~5 retries. Don't retry 400 / 401 / 403 / 404 — these are caller errors, not transient.

### "Returns 200 but doesn't actually do the thing"

A few endpoints have a **silent no-op** failure mode where the response is HTTP 200 with no error message but the requested state change didn't happen. Examples observed:

- `Orders/ChangeStatus` against a parked order — returns 200, status stays `0`. Fix: unpark first via `Orders/ChangeOrderTag`. See §8.
- JSON-bodied call to a form-only endpoint — returns 200, no change. Fix: form-encode the body. See §8.

The defence is to **read back the order state** after a write you suspect is silent-no-op-prone, until you've confirmed the recipe works.

---

## 4. Endpoint catalogue at a glance

Endpoints used in this reference, grouped by purpose.

| Endpoint | Method | Body | Purpose | Section |
|---|---|---|---|---|
| `Auth/AuthorizeByApplication` | POST | form | Get session token + cluster URL | §2 |
| `Stock/GetStockItems` | POST | JSON | Light paginated SKU list | §5 |
| `Stock/GetStockItemsFull` | POST | JSON | Hydrated items (heavy) | §5 |
| `Stock/GetStockItemsByKeys` | POST | JSON | Fetch specific items by ID array | §5 |
| `Stock/GetStockLevel` | POST | JSON | Per-location stock for one item | §5 |
| `Stock/GetStockLevel_Bulk` | POST | JSON | Per-location stock for many items | §5 |
| `Stock/GetStockLocations` | POST | JSON | List warehouse locations | §5 |
| `Inventory/GetInventoryItemSuppliers` | POST | JSON | Suppliers per SKU | §5 |
| `Categories/GetCategories` | POST | JSON | List item categories (probe before relying) | §5 |
| `Orders/CreateOrders` | POST | JSON | **Create new orders** — the big one | §7 |
| `Orders/ChangeOrderTag` | POST | **form** | Unpark a parked order | §8 |
| `Orders/ChangeStatus` | POST | **form** | Set Paid/Unpaid (1/0) | §8 |
| `Orders/GetOpenOrders` | POST | JSON | Paginated open orders | §6 |
| `Orders/GetOrdersById` | POST | JSON | Full hydrated orders by `pkOrderID` array | §6 |
| `Orders/SearchOrders` | POST | JSON | Filtered search (use for `ReferenceNum` lookups) | §6 |
| `Orders/DeleteOrder` | POST | JSON | Delete an order (singular endpoint) | §6 |
| `Orders/SetOrderShippingInfo` | POST | JSON | Write carrier + tracking number | §13 |
| `Orders/MarkOrderAsDispatched` | POST | JSON | Close order + propagate to channel | §13 |
| `PostalServices/GetPostalServices` | POST | JSON | Configured carriers + IDs | §13 |
| `Dashboards/ExecuteCustomPagedScript` | POST | **form** | Run a Query Data script (e.g. sales velocity) | §12 |
| `PurchaseOrder/Search_PurchaseOrders2` | POST | JSON | List POs by status | (out of scope) |
| `PurchaseOrder/Get_PurchaseOrder` | POST | JSON | Full PO with line items | (out of scope) |

Bold **form** rows are the ones that trip people up — see §8 and §12 for why form-encoding matters.

---

## 5. Stock & Inventory APIs

All POST + JSON unless noted.

### `Stock/GetStockItems` — paginated SKU list (light)

Returns just `(StockItemId, ItemNumber, ItemTitle)`. Use when you need to walk the catalog without per-item overhead.

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

`searchTypes`: `[0]` = SKU, `[1]` = Title, `[2]` = Barcode. Empty array = "all".

### `Stock/GetStockItemsFull` — hydrated items (heavy)

Same body shape as `GetStockItems`, plus `dataRequirements` controls which sub-objects come back:

| `dataRequirements` value | Includes |
|---|---|
| `1` | Stock levels (per location) |
| `2` | Pricing (sales channel prices) |
| `4` | Suppliers |
| `8` | Tracking |
| `16` | Images |

Pagination terminates by either:

- **Partial page**: page returns fewer than `entriesPerPage` items → that's the last page.
- **HTTP 400 after the last page**: walking past the last page raises a 400. **Treat 400 as expected end-of-catalog only when the previous page was already partial** — anywhere else, propagate as a real error.

Defensive pagination loop:

```python
items = []
page = 1
last_page_was_partial = False
while True:
    try:
        resp = call("Stock/GetStockItemsFull", json_body={
            "entriesPerPage": 200, "pageNumber": page, "dataRequirements": [1, 2],
            "loadCompositeParents": False, "loadVariationParents": False,
            "keyword": "", "searchTypes": [],
        })
    except requests.HTTPError as e:
        if e.response.status_code == 400 and last_page_was_partial:
            break    # walked off the end after a known-last partial page
        raise
    items.extend(resp)
    if len(resp) < 200:
        last_page_was_partial = True
        break
    page += 1
```

### `Stock/GetStockItemsByKeys` — fetch by ID array

Use when you have specific `StockItemId` UUIDs and want full records:

```json
{
  "request": {
    "KeysType": "StockItemId",
    "Keys": ["<linnworks_item_uuid_1>", "<linnworks_item_uuid_2>"],
    "DataRequirements": [1, 2],
    "LoadCompositeParents": false,
    "LoadVariationParents": false
  }
}
```

The exact request envelope (with or without `request` wrapper) varies by API version — probe first.

### `Stock/GetStockLevel` — per-location stock for one item

```json
{ "stockItemId": "<linnworks_item_uuid>" }
```

Returns an array, one row per stock location:

```json
[
  {
    "Location":     { "StockLocationId": "<uuid>", "LocationName": "Default" },
    "StockLevel":   42,
    "MinimumLevel": 5,
    "Available":    40,
    "InOrders":     2,
    "Due":          100,
    "BinRack":      "A1-3"
  }
]
```

### `Stock/GetStockLevel_Bulk` — per-location stock for many items

```json
{ "stockItemIds": ["<uuid_1>", "<uuid_2>"] }
```

Much faster than N individual calls.

### `Stock/GetStockLocations` — list locations

No body. Returns array of `{ StockLocationId, LocationName, IsFulfillmentCenter, ... }`. Useful at boot to discover the Default location and any FBA/3PL locations.

### `Inventory/GetInventoryItemSuppliers` — supplier details for a SKU

Returns supplier objects with `Supplier` (name), `IsDefault`, `LeadTime`, `AverageLeadTime`, `SupplierMinOrderQty`, `SupplierPackSize`, `Code`, `SupplierBarcode`, `SupplierCost`.

**Don't trust `AverageLeadTime`** — it's computed from delivery history and goes stale. Use `LeadTime` (the supplier's stated lead time).

### `Categories/GetCategories` — list categories

Documented but not load-bearing in most integrations. Probe the body shape; some tenants expose a flat list, others a tree with `ParentId` references.

### Field-name traps in stock APIs

| If you want… | Look at… | NOT… |
|---|---|---|
| Supplier's reference for this SKU | `Code` (on supplier object) | `SupplierCode` |
| Reorder point | `MinimumLevel` on `GetStockLevel` | inventory item |
| Bin location | `BinRack` on `GetStockLevel` | inventory item |
| Real lead time | `LeadTime` | `AverageLeadTime` |

### `IsVariationParent` — exclude when listing stock

Variation parents are not real stock items; only their children carry stock. When walking the catalog for sync purposes, filter out items with `IsVariationParent = true`. Otherwise you'll try to push stock to a virtual parent SKU and confuse Linnworks.

### `IsNotTrackable` — unreliable

Some tenants return `IsNotTrackable = null` for every item even when the field has a real value in the UI. Don't rely on this for filtering services from products. If you need to identify services, use a per-tenant convention (a SKU prefix, a category, a custom property).

---

## 6. Order APIs (read)

### Three identifiers per order

Linnworks orders carry three identifiers, used in different contexts:

| Identifier | Type | Source | When to use |
|---|---|---|---|
| `pkOrderID` | UUID | Linnworks-internal | All write calls. Pass arrays of these to `GetOrdersById`. |
| `NumOrderId` | integer (sequential) | Linnworks-assigned | Human-readable. Useful for log lines / Linnworks UI URLs. Don't use as a join key. |
| `ReferenceNum` (also `ExternalReference`) | string | The sales channel — channel order number | **Join key when matching against any non-Linnworks system**. Both Linnworks and the third-party see this independently. |

Rule of thumb: talking **to** Linnworks → use `pkOrderID`. Talking to anything else and looking up the corresponding Linnworks order → search by `ReferenceNum`.

### `Orders/GetOpenOrders` — paginated open-order list

Open = not yet dispatched. Body shape (verify against your tenant):

```json
{
  "entriesPerPage": 200,
  "pageNumber":     1,
  "filters":        {},
  "sorting":        []
}
```

Returns `{ Data: [<orders>], TotalEntries, TotalPages, ... }`. Each order has `pkOrderID`, `NumOrderId`, `GeneralInfo`, `ShippingInfo`, `Items`, `CustomerInfo`.

### `Orders/SearchOrders` — filtered search

For finding orders by criteria — most often `ReferenceNum`. Body shape varies by API version; **probe before relying**. A reasonable starting attempt:

```json
{
  "request": {
    "SearchTerm":     "<reference_number>",
    "SearchField":    "ReferenceNum",
    "SearchSorting":  { "SortField": "dReceivedDate", "SortDirection": "DESC" },
    "PageNumber":     1,
    "EntriesPerPage": 50
  }
}
```

If that 400s, try without the `request` wrapper.

### `Orders/GetOrdersById` — full hydrated orders

```json
{ "pkOrderIds": ["<pk_uuid_1>", "<pk_uuid_2>"] }
```

Returns header + items + customer + shipping. The standard "I have a list of pkOrderIDs from search, now get me the full records" pattern.

### `Orders/DeleteOrder` — delete a single order

Singular, not plural:

```json
{ "orderId": "<pk_uuid>" }
```

The `Orders/DeleteOrders` (plural) and `Orders/CancelOrder` variants observed during probes returned 404 — don't re-test.

---

## 7. Order creation — deep dive

This is the most consequential write operation in the API and the one with the most landmines. The locked-in shape below was confirmed end-to-end (create + verify in the UI + cleanup) against an EU-cluster tenant.

### Endpoint

```
POST /api/Orders/CreateOrders
Content-Type: application/json
Authorization: <session_token>
```

### Request body — top level

```json
{ "orders": [ <order>, <order>, ... ] }
```

**Plural**, even when sending a single order. The array under `orders` is what gets persisted. Send 1–N orders per call.

### Response shape

A **bare JSON array of pkOrderID strings**, one per order in the request, in input order:

```json
["<pk_order_id_1>", "<pk_order_id_2>"]
```

It is **not** wrapped in `{ Orders: [...] }` or `{ Data: [...] }` — top-level array of UUID strings. Don't write a parser that assumes the request shape symmetrically applies to the response.

### Required order fields

```json
{
  "Source":                  "<channel_name>",
  "SubSource":               "<sub_channel_or_external_id>",
  "ReferenceNumber":         "<unique_per_source_subsource>",
  "ExternalReferenceNumber": "<typically same as ReferenceNumber>",
  "ReceivedDate":            "<ISO 8601 datetime>",
  "DispatchBy":              "<ISO 8601 datetime, > now>",
  "LocationId":              "<stock_location_uuid>",
  "Currency":                "GBP",

  "AutomaticallyLinkBySKU":  true,
  "UseChannelTax":           true,

  "OrderItems":      [ <item>, ... ],
  "DeliveryAddress": { ... },
  "BillingAddress":  { ... }
}
```

### Critical flags — get these wrong and the order won't process

| Flag | Where | Why it matters |
|---|---|---|
| `AutomaticallyLinkBySKU: true` | Order header | Without this, Linnworks does **not** auto-link line items to stock records even when SKUs match exactly. The order lands with all lines unlinked, and tenants with the "disallow processing unlinked orders" setting can't process it at all. **Always set `true` for any integration creating orders against a real catalog.** |
| `UseChannelTax: true` | Order header | Tells Linnworks to honour the per-line `TaxRate` you sent rather than auto-calculating from product/category defaults. Without this, your tax values are ignored. |
| `UseChannelTax: true` | **Per line item** | Same flag, also required at line-item level. Set both. |
| `TaxCostInclusive: true` | Per line item | Tells Linnworks the line price already includes VAT, so it back-calculates net + VAT from gross. Without this, Linnworks adds VAT on top of your already-inclusive price → 20% over-charge for VAT-registered products. |

### Order item fields

```json
{
  "SKU":          "<linnworks_sku>",
  "ChannelSKU":   "<typically same as SKU>",
  "ItemNumber":   "<typically same as SKU>",
  "ItemTitle":    "<line description>",
  "Qty":          1,
  "PricePerUnit": 9.99,
  "Discount":     0,
  "LineDiscount": 0,

  "TaxRate":          20,
  "TaxCostInclusive": true,
  "UseChannelTax":    true,

  "isService":   false,
  "StockItemId": "<linnworks_item_uuid>"
}
```

Field-by-field notes:

- **`SKU`** — the Linnworks-side SKU. Must match exactly (case-sensitive in some tenant configs).
- **`ChannelSKU`** — the channel-side SKU. Typically the same string as `SKU` for direct-source integrations. Can differ when the channel rewrites SKUs.
- **`ItemNumber`** — usually identical to SKU. Some tenants distinguish; safest is to set them all equal.
- **`Qty`** — integer, ≥ 1.
- **`PricePerUnit`** — decimal in the order's currency. **Per-unit, not line total** — Linnworks computes line total from `PricePerUnit × Qty`.
- **`StockItemId`** — the Linnworks-internal UUID for the stock record. **Setting this on the line is what makes it "linked" in the UI.** Without `StockItemId` (and even with `AutomaticallyLinkBySKU: true`), the line shows as "Unlinked item" and may be unprocessable.
  - **Only set when you actually have a UUID.** Empty string or null is rejected; a fabricated UUID would silently mis-link.
  - If you don't have one (service line, ad-hoc line), omit the field entirely and set `isService: true`.
- **`isService`** — `true` for service / non-stock-tracked lines. Tells Linnworks not to attempt stock linking and not to decrement on processing. `false` for real stock items.
- **`TaxRate`** — VAT rate as a percentage integer (`20` for UK standard, `0` for exempt). See §11.

### Duplicate SKUs across line items — rejected

`Orders/CreateOrders` **rejects an order where the same SKU appears on more than one `OrderItem`**. Each SKU must appear on exactly one line, with the quantity summed. The rejection is an HTTP 400 with a body like:

```json
{"Code": null,
 "Message": "Orders have invalid values: Order: <orderId>, LineId: <SKU> is duplicated"}
```

(Observed live on order `dCxw8888clDsEA5t5SbkvNcOaFEZY`: SKU `SPR22` rang up on two separate Square POS lines.)

If your source system (e.g. a POS till) allows the same item to be rung up on multiple separate lines, **merge same-SKU lines before sending**: combine into one `OrderItem` with the summed `Qty`, and derive `PricePerUnit` from the *summed line totals* (Σ `Qty × PricePerUnit` ÷ Σ `Qty`) rather than a naive per-unit mean — that preserves correctness when one line carried a partial discount. The merge key is the SKU value itself, so it applies equally to title-as-SKU fallback lines for services / ad-hoc items; the duplicate check is on the SKU string regardless of stock-link status.

In this repo, `tools/pull_square_orders_to_linnworks.py` does exactly this via `_merge_duplicate_skus()`, called in `_build_linnworks_payload` just before the `OrderItems` list is finalised. Proof test: `tests/test_merge_duplicate_skus.py`.

### Strong-link vs weak-link

There are three states a line item can land in:

| State | `AutomaticallyLinkBySKU` | `StockItemId` set? | SKU matches a stock record? | Behaviour |
|---|---|---|---|---|
| Strong-linked | `true` | yes | yes | Line links to the inventory record. Stock decrements on processing. **The desired state.** |
| Weak-linked | `true` | no | yes | Linnworks resolves SKU → stock record server-side. Equivalent to strong-linked at the order level, but you didn't tell it which UUID — it had to look up. Slightly slower; brittle if the SKU is ambiguous. |
| Unlinked | either | no | no | Line shows as "Unlinked item". Tenants with strict process settings can't dispatch the order. |

For integrations creating orders programmatically, **always strong-link**: pre-resolve `SKU → StockItemId` (cache it in your own DB), and send both. This avoids the resolve-at-write-time race where a SKU might be missing from the catalog.

### Address fields

```json
{
  "FullName":     "<recipient_name>",
  "Company":      "",
  "EmailAddress": "<email>",
  "PhoneNumber":  "<phone>",
  "Address1":     "<street>",
  "Address2":     "",
  "Address3":     "",
  "Town":         "<city>",
  "Region":       "<county_or_state>",
  "PostCode":     "<postal_code>",
  "Country":      "United Kingdom",
  "CountryCode":  "GB"
}
```

The address must be named **`DeliveryAddress`**, NOT `ShippingAddress`. Misnaming is a silent 400 cause that's easy to miss because the docs sometimes use the wrong name.

`BillingAddress` is the same shape and may be a copy of `DeliveryAddress`.

**Anonymous orders work fine** — there's no separate "create customer" API call required. Embed the customer's name/email/phone directly in the address objects. Linnworks does not require a pre-existing customer record.

For POS-style sales where you have no shipping recipient, fall back to a placeholder (the shop's own address with a generic "Walk-in Customer" name). Linnworks doesn't reject placeholder addresses; it just stores what you sent.

### Country handling

`CountryCode` is the ISO 3166-1 alpha-2 code (`"GB"`, `"US"`, etc.). `Country` is the full name (`"United Kingdom"`, `"United States"`). Both are required — supply a small ISO-code → name map in your integration:

```python
COUNTRY_NAME = {
    "GB": "United Kingdom",
    "US": "United States",
    "IE": "Ireland",
    "FR": "France",
    "DE": "Germany",
    # ... extend as needed
}
```

Fall back to the code itself if the lookup misses.

### Natural deduplication

Linnworks dedupes `Orders/CreateOrders` calls on the **`(Source, SubSource, ReferenceNumber)` triple**. Re-submitting the same triple returns the same `pkOrderID` — no error, no duplicate row.

This means: **derive `ReferenceNumber` deterministically from your external order id** (e.g. the channel's order UUID). Retries are then naturally idempotent — if your worker crashes between step 1 (create) and step 2 (unpark), the next run re-creates with the same triple, gets the same `pkOrderID` back, and continues from step 2.

You don't need to track "did this create succeed?" separately — just record the bookkeeping row after **all** the steps you care about have succeeded, and let Linnworks' natural dedup handle in-flight failures.

### Direct-source orders land parked

Orders created via `Orders/CreateOrders` from a `Source = "DIRECT"` (or any custom source not registered as a channel integration) land with **`IsParked: true`**. Parked orders don't show in the default Open Orders queue and cannot be transitioned via `ChangeStatus` until unparked.

This is by design: Linnworks parks unfamiliar-source orders for manual review. For an integration that wants the order ready-to-fulfil immediately, follow the 3-step recipe in §8.

### Working payload example

```json
{
  "orders": [
    {
      "Source":                  "POS",
      "SubSource":                "# <external_order_id>",
      "ReferenceNumber":         "<external_order_id>",
      "ExternalReferenceNumber": "<external_order_id>",
      "ReceivedDate":            "2026-05-06T10:30:00+00:00",
      "DispatchBy":              "2026-05-08T10:30:00+00:00",
      "LocationId":              "00000000-0000-0000-0000-000000000000",
      "Currency":                "GBP",
      "AutomaticallyLinkBySKU":  true,
      "UseChannelTax":           true,
      "OrderItems": [
        {
          "SKU":               "WIDGET-001",
          "ChannelSKU":        "WIDGET-001",
          "ItemNumber":        "WIDGET-001",
          "ItemTitle":         "Widget, blue",
          "Qty":               1,
          "PricePerUnit":      11.99,
          "Discount":          0,
          "LineDiscount":      0,
          "TaxRate":           20,
          "TaxCostInclusive":  true,
          "UseChannelTax":     true,
          "isService":         false,
          "StockItemId":       "<linnworks_item_uuid>"
        }
      ],
      "DeliveryAddress": {
        "FullName":     "Walk-in Customer",
        "Company":      "",
        "EmailAddress": "orders@example.com",
        "PhoneNumber":  "0700000000",
        "Address1":     "123 Example Street",
        "Address2":     "",
        "Address3":     "",
        "Town":         "Anytown",
        "Region":       "",
        "PostCode":     "AA1 1AA",
        "Country":      "United Kingdom",
        "CountryCode":  "GB"
      },
      "BillingAddress": {
        "...": "same shape as DeliveryAddress"
      }
    }
  ]
}
```

---

## 8. Order state transitions — the 3-step recipe

After `Orders/CreateOrders`, an order from a direct/unregistered source is in this state:

| Field | Value | Meaning |
|---|---|---|
| `GeneralInfo.IsParked` | `true` | Won't show in Open Orders queue |
| `GeneralInfo.Status`   | `0`    | Unpaid |
| `Processed`            | `false` | Not dispatched |

To make the order **ready to fulfil and visible in the Open Orders queue**, you need two more calls. Together they form a **3-step recipe** for "create a new order that's already paid and waiting to be processed":

```
Step 1: Orders/CreateOrders        (JSON)            → returns pkOrderID
Step 2: Orders/ChangeOrderTag      (form-encoded)    → unparks
Step 3: Orders/ChangeStatus        (form-encoded)    → marks paid (status=1)
```

### Why this exact order

- **Step 2 must precede step 3.** `ChangeStatus` against a parked order **silently no-ops**: HTTP 200, no error, but `Status` stays `0`. This is a no-error-message failure mode that took multiple iterations to diagnose. Always unpark first.
- **Step 1 alone is not enough** for direct-source orders — they're parked, invisible to the default fulfilment queue.
- **Only steps 2 and 3 together** transition the order through `parked → unparked → paid`. The "paid" status is what fulfilment staff see when they go to process the order.

### Step 2 — `Orders/ChangeOrderTag` (unpark)

```
POST /api/Orders/ChangeOrderTag
Content-Type: application/x-www-form-urlencoded
Authorization: <session_token>

orderIds=%5B%22<pk_uuid>%22%5D
```

The form value for `orderIds` is a **JSON-encoded array as a string** — the literal characters `["<uuid>"]` (brackets and quotes part of the value), then URL-encoded by the HTTP client. This is the same encoding trick used by `Dashboards/ExecuteCustomPagedScript`'s `parameters` field (§12).

In Python:

```python
import json
form = {"orderIds": json.dumps([pk_order_id])}
requests.post(url, headers=headers, data=form, timeout=60)
```

No other fields. The endpoint name itself implies the unpark action — there's no `parked: false` parameter.

Response: a bare JSON array containing the `pkOrderID`. Treat success purely by HTTP status; the array is informational.

### Step 3 — `Orders/ChangeStatus` (mark paid)

```
POST /api/Orders/ChangeStatus
Content-Type: application/x-www-form-urlencoded
Authorization: <session_token>

orderIds=%5B%22<pk_uuid>%22%5D&status=1
```

Same form-encoding convention. The `status` field takes the enum:

| `status` value | Meaning |
|---|---|
| `0` | Unpaid |
| `1` | Paid |

In Python:

```python
form = {"orderIds": json.dumps([pk_order_id]), "status": "1"}
requests.post(url, headers=headers, data=form, timeout=60)
```

Linnworks **automatically stamps `PaidDateTime` server-side** when `Status` flips to `1`. You don't set it.

### Verified post-call state

After all three steps, reading the order back via `Orders/GetOrdersById`:

| Field | Value | Meaning |
|---|---|---|
| `GeneralInfo.IsParked` | `false` | In the Open Orders queue |
| `GeneralInfo.Status`   | `1`    | Paid |
| `Processed`            | `false` | Awaiting dispatch — fulfilment staff process manually |
| `TotalsInfo.PaidDateTime` | `<server-stamped timestamp>` | Linnworks set this |

### JSON-bodied call to `Orders/ChangeStatus` — silent no-op

If you accidentally send the body as JSON instead of form-encoded:

```python
# DO NOT — looks fine, returns 200, doesn't change state
requests.post(url, headers={..., "Content-Type": "application/json"},
              json={"orderIds": [pk], "status": 1})
```

The endpoint returns HTTP 200, but the order state doesn't change. There is no error message. Use form encoding (`data=`, not `json=`).

### Recovery semantics for the 3-step recipe

A common failure pattern is partial success: step 1 lands but step 2 or 3 fails (network blip, rate limit, transient 500). The recipe is naturally retry-safe:

- **Don't insert your own bookkeeping row until all three steps succeed.** Bookkeeping is the "I'm done with this order" marker.
- **On retry**, step 1 re-creates with the same `(Source, SubSource, ReferenceNumber)` triple → Linnworks dedup returns the same `pkOrderID` → cheap. Steps 2 and 3 are idempotent against an already-unparked / already-paid order (no error if state is already there).
- **Log per-step success/failure** so you can diagnose stuck orders. A recurring "create OK, unpark failed" pattern points at a tenant config issue (e.g. an order tag that requires special permissions to remove).

### Handling persistent failures — the retry table pattern

Retry-on-next-run (above) is only safe while the failing order stays
*in the pull window*. The order-pull watermark advances to
`max(updated_at)` of the **successful** orders in a batch — so if one
order fails while newer orders in the same batch succeed, the watermark
drags past the failure and it's never re-fetched. It's silently
stranded forever. (Observed live: Square order `dCxw8888…` failed
Linnworks' duplicate-SKU validation while later orders succeeded; the
watermark skipped it.)

The fix is a dedicated retry table, `sq_orders_failed`, that decouples
failed-order handling from the watermark entirely:

| Column | Role |
|---|---|
| `square_order_id` (PK) | Idempotency key — one row per failing order. |
| `first_failed_at` / `last_attempted_at` | Failure window for triage. |
| `attempts` | Incremented on every re-failure. |
| `last_error` | Most recent error message. |
| `square_order_json` | The full Square order, captured on **first** failure (canonical — never overwritten). Lets the cron re-attempt without re-fetching from Square. |
| `stuck` | `TRUE` once `attempts >= 5`. Removes the order from auto-retry. |
| `stuck_notified_at` | Stamped when the one-time escalation email succeeds. |

**Flow:**

1. **On a CreateOrders failure** — upsert the order into
   `sq_orders_failed`. First failure inserts `attempts = 1` + the full
   JSON; a repeat failure increments `attempts` and refreshes
   `last_error` / `last_attempted_at` *without* overwriting the stored
   JSON.
2. **Retry pass** — at the start of every run, after the normal Square
   fetch, load all non-stuck rows, reconstruct each order from
   `square_order_json`, and merge them into the to-process list (dedup
   by `square_order_id` against the fresh fetch). They flow through the
   exact same build/create path as fresh orders.
3. **On a CreateOrders success** — `DELETE` the row. The order resolved
   itself; the table only holds genuinely-failing orders.
4. **Escalation** — the moment `attempts` reaches **5**, flip
   `stuck = TRUE`, send **one** alert email (via Resend) and stop
   auto-retrying. Stuck rows are excluded from the retry pass and sit
   for human triage. The email fires exactly once per stuck order
   (guarded by the `stuck` flag transition, recorded by
   `stuck_notified_at`).

The watermark logic is **unchanged** — it still advances on
`max(updated_at)` of successful orders. That's the point: the retry
table makes the watermark free to advance without ever stranding a
failure.

---

## 9. Stock processing flow

### When does stock decrement?

**Stock is NOT deducted on order creation. Stock is NOT deducted on mark-paid.** Stock decrements only when an order is **Processed** — a separate state transition that happens manually (warehouse staff click "Process") or via `Orders/MarkOrderAsDispatched` (see §13).

| State transition | Stock decrement? |
|---|---|
| `CreateOrders` → order exists | No |
| `ChangeOrderTag` → unparked | No |
| `ChangeStatus(1)` → paid | No |
| `MarkOrderAsDispatched` (or manual Process click) | **Yes** |

This is opposite to many ecommerce platforms where stock holds happen at cart/checkout. In Linnworks, the order can sit in the Open Orders queue with stock unaffected until someone actually dispatches it.

Implications for integrations:

- **Don't expect channel stock levels to track Linnworks immediately after an order lands.** There's a window between order creation and processing.
- **Stock-push direction matters:** if Linnworks is your source of truth, push deltas after processing happens, not after order creation. (Or just push on a regular cadence — see §14.)

### "Mark as paid" is a financial state, not a stock state

`Status = 1` (Paid) records that the customer has paid for the order. It does not move stock and does not close the order. An order can sit Paid+UnProcessed for days while warehouse staff work through the queue.

### The full happy-path lifecycle

```
[create]                    →  parked, unpaid, unprocessed
[unpark]                    →  open,   unpaid, unprocessed
[mark paid]                 →  open,   paid,   unprocessed
[process / dispatch]        →  closed, paid,   processed   ← stock decrement here
```

For an integration that creates orders from an external POS, the integration owns the first three transitions (the 3-step recipe in §8). The processing step is owned by warehouse staff — automating it is possible (`MarkOrderAsDispatched`) but usually undesirable: warehouse staff want to verify pick/pack physically before declaring the order shipped.

---

## 10. Channel naming: Source / SubSource / ReferenceNumber

Linnworks classifies every order with a three-part identifier:

| Field | Purpose | Example |
|---|---|---|
| `Source` | Top-level integration / channel name | `"SHOPIFY"`, `"AMAZON"`, `"EBAY"`, `"POS"`, `"DIRECT"` |
| `SubSource` | Sub-channel or external reference | Channel-account name; or `"# <external_order_id>"` for direct integrations |
| `ReferenceNumber` | The specific order's identifier within `Source/SubSource` | The channel's order id |

### `Source` conventions

For first-party channel integrations Linnworks knows about (Shopify, Amazon, eBay), it uses canonical names. For direct integrations you build:

- Pick a stable identifier for the **integration itself**, not the specific tenant or marketplace.
- ALL_CAPS is the convention; alphanumeric + underscores only.
- Examples: `"POS"` for a point-of-sale integration, `"WEBSITE"` for a custom storefront, `"DIRECT"` for ad-hoc/manual orders.

### `SubSource` conventions

Used for either:

- **Sub-channel naming**: when one `Source` covers multiple distinct accounts/regions (e.g. `Source = "AMAZON"`, `SubSource = "Amazon UK"` vs `"Amazon DE"`).
- **External-order tagging** (the "`# <id>`" pattern): when each order is from a distinct external system but the integration is a single channel. Example: `Source = "POS"`, `SubSource = "# abc123-def456"` where `abc123-def456` is the POS-side order id. This makes the source value globally unique per order, which has UX benefits in the Linnworks UI (the order detail page shows "POS" + the external id at a glance).

### `ReferenceNumber` conventions

The order's identifier within `(Source, SubSource)`. Combined with `Source` and `SubSource`, this triple is the natural dedup key (§7).

For deterministic idempotency: derive `ReferenceNumber` from the external system's order id directly. Don't generate UUIDs in your integration — use the upstream id verbatim.

### `ExternalReferenceNumber`

A second free-text field that accepts whatever the channel originally sent. For direct integrations, set it equal to `ReferenceNumber`. Some channels populate this differently (e.g. Amazon's MerchantOrderID vs SellerOrderID).

---

## 11. VAT & tax handling

For UK VAT-registered integrations, the recipe is:

```json
{
  "TaxRate":           20,
  "TaxCostInclusive":  true,
  "UseChannelTax":     true
}
```

Set this on **every order item** plus `UseChannelTax: true` at the order header level.

### What each flag does

| Flag | Effect |
|---|---|
| `TaxRate: 20` | The VAT rate as a percentage (integer). Use `20` for UK standard, `5` for reduced (energy, etc.), `0` for exempt items (services, books, children's clothing). |
| `TaxCostInclusive: true` | Tells Linnworks the `PricePerUnit` already includes VAT. Linnworks back-calculates net + VAT from gross. **This is what you want when your channel sends VAT-inclusive prices** (most retail systems). |
| `TaxCostInclusive: false` | The default. `PricePerUnit` is treated as net, VAT is added on top. If your channel sends inclusive prices and you leave this `false`, your orders get charged 20% over what the customer paid. |
| `UseChannelTax: true` | Tells Linnworks to honour the per-line `TaxRate` you sent rather than overriding from product/category defaults. Set at **both** order and line-item level. |

### Worked example

Channel sells a widget for `£11.99` VAT-inclusive at standard rate.

Send to Linnworks:

```json
{
  "PricePerUnit":     11.99,
  "TaxRate":          20,
  "TaxCostInclusive": true,
  "UseChannelTax":    true
}
```

Linnworks records:

| Field | Value |
|---|---|
| Net unit price | `£9.99` |
| VAT per unit | `£2.00` |
| Gross unit price | `£11.99` |

If `TaxCostInclusive` were `false` (or missing), Linnworks would treat `£11.99` as net and record `£14.39` gross — the customer was charged `£11.99` but the order ledger shows `£14.39`. Reconciliation hell.

### Services

UK services are usually exempt or zero-rated. Send:

```json
{
  "PricePerUnit":     50.00,
  "TaxRate":          0,
  "TaxCostInclusive": true,
  "UseChannelTax":    true,
  "isService":        true
}
```

`TaxCostInclusive: true` with `TaxRate: 0` is equivalent to "no VAT applies"; Linnworks records `£50.00` net = `£50.00` gross.

### Mixed-rate orders

Some orders contain both standard-rated products and zero-rated services. Set `TaxRate` per-line as appropriate; `UseChannelTax: true` at order level + per-line tells Linnworks to honour each line's rate independently.

### Non-UK tax regimes

Linnworks supports per-country tax. The `TaxRate` field is rate-as-integer regardless of country. For non-UK orders, populate the `DeliveryAddress.CountryCode` correctly and set the rate that applies in the destination country (or `0` for export).

---

## 12. Sales / velocity APIs

The right path for sales-velocity / units-sold queries is **`Dashboards/ExecuteCustomPagedScript`** — Linnworks' built-in "Query Data" script runner. Treat it like a parametrised stored procedure.

### Endpoint shape

```
POST /api/Dashboards/ExecuteCustomPagedScript
Content-Type: application/x-www-form-urlencoded
```

**Form-urlencoded, not JSON.** Body fields:

| Field | Type | Notes |
|---|---|---|
| `scriptId` | string | Numeric script id (see catalogue link below) |
| `entriesPerPage` | string | e.g. `"500"` |
| `pageNumber` | string | 1-indexed |
| `parameters` | string | JSON-encoded **string** of an array of `{Type, Name, Value}` objects |

The `parameters` field is recursively encoded: it's a string in form-encoding terms, but the string value is JSON. In Python:

```python
import json, requests

form_body = {
    "scriptId":       "47",
    "entriesPerPage": "500",
    "pageNumber":     "1",
    "parameters":     json.dumps([
        {"Type": "Date", "Name": "startDate", "Value": "2026-01-30"},
        {"Type": "Date", "Name": "endDate",   "Value": "2026-04-30"},
    ]),
}

resp = requests.post(
    f"{server}/api/Dashboards/ExecuteCustomPagedScript",
    data=form_body,                                       # <-- data=, not json=
    headers={"Authorization": session_token},
    timeout=120,
)
```

### Param names are case-sensitive and per-script

The error message when wrong is helpfully explicit:

```
"Expected parameter 'endDate' to exist."
```

— which tells you both that the param is required and exactly what name to use. **Read 400 error messages carefully on this endpoint** — they often pinpoint the issue.

### Script catalogue varies per tenant

What's listed in Linnworks' public script catalogue is not a guarantee your tenant has the same set. Always probe. Reference: <https://help.linnworks.com/support/solutions/articles/7000018696>.

### "Stock/GetStockSold" is a dead end for velocity

Despite the name, `Stock/GetStockSold` is **not** a velocity endpoint. It returns "items also bought" — SKUs that co-occurred on orders alongside the queried `stockItemId`. A market-basket correlation, not a units-sold report. For actual sales velocity, use `Dashboards/ExecuteCustomPagedScript`.

### Composite/kit attribution caveat

The `SKU` field on each row of a velocity script result is **whatever was on the order line at sale time**. For composite/kit products, this can be either:

- the **kit SKU** (the kit shipped as a single unit), or
- a **component SKU** (the kit was exploded into its components at sale time)

Behaviour depends on the channel and the kit configuration. Linnworks doesn't normalise this. If you need full per-component depletion, cross-join against your BOM in your own logic.

There's no risk of double-counting because a given sale only logs one of the two — kits that ship intact appear under the kit SKU, kits that explode appear under component SKUs. Sum across both bands cleanly.

---

## 13. Writing tracking & dispatch

Best-known shapes — **probe before committing to production code**.

### `Orders/SetOrderShippingInfo` — write carrier + tracking

```json
{
  "orderId": "<pk_order_id>",
  "info": {
    "Vendor":            "<carrier_name>",
    "PostalServiceName": "<service_name>",
    "PostalServiceId":   "<linnworks_postal_service_uuid>",
    "TrackingNumber":    "<tracking_string>",
    "Weight":            <kg_as_number>,
    "TotalWeight":       <kg_as_number>
  }
}
```

`PostalServiceId` may be optional if `PostalServiceName` resolves uniquely on the tenant; probe both.

### `Orders/MarkOrderAsDispatched` — close the order

```json
{
  "orderId":         "<pk_order_id>",
  "dispatched_date": "<ISO 8601 datetime>"
}
```

**This is the call that triggers Linnworks' dispatch propagation** to the source channel (Shopify gets fulfilled, Amazon gets a tracking notification, eBay marks as shipped). Without this call, the tracking number sits on the order in the Linnworks UI but the channel never finds out.

If your integration just stops at "tracking is on the Linnworks order but it's still 'open'", you've won half — Linnworks will surface the tracking in its UI. To actually mark as shipped on the channel, follow up with `MarkOrderAsDispatched`.

### `PostalServices/GetPostalServices` — list configured carriers

Returns the carrier+service pairs configured on the tenant with their `PostalServiceId` UUIDs. Use this once at boot to build a `carrier_name → PostalServiceId` map for your tracking writes.

### Idempotency for tracking writes

If your job runs every N minutes and pulls all "shipped today" shipments, you'll re-process the same shipment many times. Defences:

- **Read first**, write only if `ShippingInfo.TrackingNumber` is empty (or differs and you want to overwrite).
- **Audit log** every successful write with `(external_shipment_id, linnworks_order_id, tracking_number, written_at)`. Skip shipments already in the log.

The audit-log approach is more robust to manual edits (someone adds a tracking number by hand; your job won't clobber it).

---

## 14. Recommended integration architecture

### The two-cron pattern

Most Linnworks integrations have two natural directions:

1. **Catalog → Channel** (push stock/price/items from Linnworks to wherever you're selling).
2. **Orders → Linnworks** (pull sales from the channel and create orders in Linnworks).

Build these as **two independent scheduled jobs**, not one combined daemon:

| Job | Direction | Cadence | Why |
|---|---|---|---|
| Catalog sync | Linnworks → Channel | Every 15–30 min | Stock levels are the slowest-changing thing customers care about. 30 min is acceptable for most retail. |
| Order pull | Channel → Linnworks | Every 5 min | Orders are time-sensitive — POS staff want to see the order in Linnworks within minutes. |

**Why two jobs**:

- **Independent failure domains.** If catalog-sync breaks, orders still flow. If order-pull breaks, stock still updates.
- **Different cadences.** Stock can lag 30 min; orders can't.
- **No long-running daemon to babysit.** Each job is a stateless cron run.

### Where to host

Cron-on-a-cloud-host is fine; what matters is **deterministic scheduling**.

- Avoid free-tier scheduling that throttles silently. Some platforms' free tier `*/5` schedules deliver 30+ minutes late under load. The integration tolerance budget for orders is usually < 10 minutes — a free-tier 30-minute lag is unacceptable.
- Pay for deterministic cron (Railway, Fly.io, a small VM with cron, etc.) rather than chasing free-tier flakiness. The cost is small relative to debugging "why is the order lag 50 minutes today".
- Use a **shared Docker image** for both jobs — different start command, different schedule, same code.

### State management — what you persist

A small Postgres / Supabase database is enough. Five tables:

| Table | Purpose |
|---|---|
| **SKU map** | One row per SKU. Caches `sku → linnworks_stock_item_id` (UUID) plus the last-known stock level / price for no-op skipping. Populated by catalog-sync; read by order-pull to strong-link line items. |
| **Watermarks** | Key-value cursors. Order-pull stores `last_processed_updated_at`; catalog-sync stores `last_full_sync_at`. |
| **Processed orders** | Idempotency record for order-pull. One row per external order id processed. Skip duplicates on retry. |
| **Errors** | Per-error log for non-fatal failures during a run. `(timestamp, job, message, context_json)`. |
| **Run audit** | One row per cron execution. `(job, started_at, finished_at, mode, fetched, processed, skipped, failed)`. Useful for dashboards and post-mortems. |

The SKU map is the most important table. **Linnworks is slow to walk for SKU resolution on every order** — a 100-order day with one `Stock/GetStockItems` call per order is 100 × ~1s = 100 seconds of API time, which blows your `*/5` budget.

### Watermark-based polling

Order-pull pattern:

1. Read watermark `last_processed_updated_at`. If absent, default to "7 days ago" for the initial backfill.
2. Pull from channel: `updated_at >= watermark - safety_buffer` (60s buffer handles eventual-consistency drift).
3. For each order: check Processed Orders table for idempotency, skip if already done.
4. Build Linnworks payload, run the 3-step recipe.
5. On success, insert into Processed Orders and advance the watermark to `max(updated_at)` of successful orders.
6. On partial failure, **don't advance the watermark past failed orders** — let the next run retry.

The 60s safety buffer matters: channels (especially eventually-consistent ones) sometimes return an order with `updated_at = T` after the watermark is already at `T+30s`. The buffer makes the next run re-evaluate and skip via the idempotency table — slow but correct.

### Audit log granularity

Every cron run should write exactly **one summary row** to a run audit table, regardless of mode (observe/write). Counts to track:

```
fetched     — pulled from upstream
processed   — successfully completed all steps
skipped     — already in idempotency table
skipped_*   — domain-specific skips (empty orders, malformed, etc.)
failed      — attempted but failed (with error_summary truncated to ~1KB)
```

Plus per-step counts for multi-step recipes (e.g. `created`, `unparked`, `marked_paid` for the 3-step). Per-step counts let you spot stuck states (`created=N, unparked=N, marked_paid=N-2` = two orders are stuck after step 2).

### Observe-mode flag is non-negotiable

Every tool that writes to Linnworks should have a `--write` flag (default off). Without it:

- Print the planned payload for the first few items.
- Don't make any Linnworks write calls.
- Don't advance the watermark.
- Don't insert into idempotency / processed-orders tables.
- **Do** write the audit-log row (mode=observe), so observe runs are visible in the dashboard alongside writes.

This pattern is what keeps "let me dry-run this real quick" safe. It's also what you fall back to when something goes wrong in production — `--write` off, look at what it would do, diagnose without committing.

---

## 15. Diagnostic-first development pattern

Linnworks' API surface is wide, inconsistently documented, and varies between tenants. **Probe before you write production code.** This pattern has saved hours on every endpoint:

### The pattern

1. **Write a diagnostic-only script** that hits the candidate endpoint with multiple body / parameter shapes.
2. **Log everything**: HTTP status, response body (truncated to ~500 chars on error), the field-name summary of any successful response.
3. **Run it via a manual-trigger CI job** (e.g. GitHub Actions `workflow_dispatch`), not from a laptop — so credentials stay in CI secrets and never touch local env files.
4. **Read error messages.** Linnworks' 400s often tell you the exact param name expected, the exact type expected, or that the script doesn't exist on this tenant.
5. **Lock in via commit.** Once a working shape is found, commit it (and keep the diagnostic file in the repo for the next debugging session).

### Probe script skeleton

```python
import json, os, sys, requests

AUTH_URL = "https://api.linnworks.net/api/Auth/AuthorizeByApplication"


def authorize(app_id, app_secret, install_token):
    resp = requests.post(AUTH_URL, data={
        "ApplicationId":     app_id,
        "ApplicationSecret": app_secret,
        "Token":             install_token,
    }, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    return body["Token"], body["Server"]


def probe(server, token, path, *, method="POST", body=None, form=None,
          params=None, label=""):
    url = f"{server}/api/{path}"
    print(f"\n--- {label} ---")
    print(f"{method} {url}")
    if body  is not None: print(f"JSON body: {json.dumps(body)[:300]}")
    if form  is not None: print(f"Form body: {form}")
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

### Probes are read-only by convention

Probes should never write — they only read and print. Keep destructive actions behind a separate `--write` flag in the production tool, never in the probe. A probe that writes is a probe that's been promoted to a production tool and should be renamed.

### Diagnostic markers for findings

When a probe confirms an endpoint shape, print a line prefixed with a distinctive marker, e.g. `=== DISCOVERY: ===`. Then a single grep across the workflow log surfaces every locked-in fact. Paste those into the project's discoveries doc and commit.

---

## 16. Common gotchas (catch-all)

A flat list of every "wait, what?" moment encountered. Skim before debugging anything weird.

**Auth & cluster**

- 401/403 mid-run usually means session token expired. Re-auth and retry once.
- Wrong cluster URL returns silent 404 on every endpoint. Always use `Server` from the auth response.
- `Bearer` prefix on the `Authorization` header → 401. Use the raw session token.

**Pagination**

- HTTP 400 on `Stock/GetStockItemsFull` page N+1 usually means "end of results". Treat as expected only if page N was already partial; otherwise propagate.

**Body shapes**

- `Orders/CreateOrders` request: `{ orders: [order] }` (plural array). Response: bare array of `pkOrderID` strings (not wrapped). The two shapes don't match.
- Form-encoded endpoints expect `orderIds=["uuid"]` style (JSON-string-of-array, then URL-encoded). JSON-encoded endpoints expect `{orderIds: ["uuid"]}`. Don't mix them.

**Order linking**

- Without `AutomaticallyLinkBySKU: true`, line items don't link even with matching SKUs.
- Without `StockItemId` on the line, the link is "weak" (resolved server-side from SKU); ambiguous SKUs may fail to resolve.
- "Unlinked items" cannot be processed in tenants with the strict-process-unlinked setting on. Surfaces as "order won't process" in the UI.

**Order state**

- `Orders/ChangeStatus` against a parked order silently no-ops (HTTP 200, no error, status unchanged). Always unpark first via `Orders/ChangeOrderTag`.
- JSON-bodied call to `Orders/ChangeStatus` silently no-ops. Use form encoding.
- Linnworks auto-stamps `PaidDateTime` server-side when `Status` flips to `1`. Don't try to set it.

**Tax**

- Without `TaxCostInclusive: true`, `PricePerUnit` is treated as net and VAT is added on top. If your channel sends VAT-inclusive prices, this over-charges by 20%.
- `UseChannelTax: true` must be set at **both** order header and per-line-item level. Setting it at one level only gets ignored.

**Field names**

- Address must be `DeliveryAddress`, NOT `ShippingAddress`. Misnaming → silent 400.
- Supplier reference is `Code`, not `SupplierCode`.
- Reorder point is on `Stock/GetStockLevel`, not on the inventory item.

**Settings that aren't visible to the API consumer**

- "Disallow processing unlinked orders" tenant setting changes whether unlinked lines block fulfilment. You can't see this setting via the API; ask the tenant admin.
- Order tags (parked / unparked / others) can have permission rules that prevent your app from removing them. Surfaces as "ChangeOrderTag returns 200 but the tag stays". Probe in observe mode.

**Performance**

- Walking the entire stock catalog on every run is slow (~1 req/sec per page × N pages). Cache `sku → StockItemId` in your own DB and refresh on a slower cadence.
- `Stock/GetStockLevel_Bulk` is much faster than N × `Stock/GetStockLevel`. Always batch when you can.

**Deduplication**

- Re-submitting `Orders/CreateOrders` with the same `(Source, SubSource, ReferenceNumber)` triple returns the same `pkOrderID`. No error, no duplicate. **Lean on this**: derive `ReferenceNumber` deterministically from your external id and retries become free.

---

## 17. Endpoints tried and ruled out

Save future probe time — these returned 404 or never produced a working body shape on observation. Don't re-test unless you have reason to believe Linnworks has shipped a fix.

| Endpoint | Status | Note |
|---|---|---|
| `Orders/SetPaymentStatus` | 404 | Multiple body shapes attempted. The real mark-paid endpoint is `Orders/ChangeStatus`. |
| `Orders/AddOrderPayment` | 404 | — |
| `Orders/SetOrderPayment` | 404 | — |
| `Orders/PayOrder` | 404 | — |
| `Orders/SetOrderParkedStatus` | 404 | The obvious-sounding unpark endpoint doesn't exist. Real one is `Orders/ChangeOrderTag`. |
| `Orders/CreateNewOrder` (singular) | Wrong endpoint | Creates an empty draft order. Use `Orders/CreateOrders` (plural). |
| `Orders/DeleteOrders` (plural) | 404 | Use the singular `Orders/DeleteOrder`. |
| `ProcessedOrders/SearchProcessedOrdersPaged` | 400 | Returned 400 on every body shape attempted (flat, request-wrapped, with/without `SearchSorting`). Either deprecated or signature changed; didn't pursue. Use `Dashboards/ExecuteCustomPagedScript` for sales history. |
| JSON body to `Orders/ChangeStatus` | Silent no-op | Returns 200 but doesn't change state. Endpoint is form-encoded only. |
| `Stock/GetStockSold` for velocity | Wrong purpose | Returns "items also bought" co-occurrence, not units sold. Use `Dashboards/ExecuteCustomPagedScript` Script 47 (or your tenant's equivalent). |

---

## Appendix A — Common request patterns

```python
# Auth — form-urlencoded; response gives session token + cluster URL
requests.post(
    AUTH_URL,
    data={"ApplicationId": ..., "ApplicationSecret": ..., "Token": ...},
)

# Standard JSON call — most endpoints
requests.post(
    f"{server}/api/Some/Endpoint",
    headers={"Authorization": session_token, "Content-Type": "application/json"},
    json={"key": "value"},
)

# Form-urlencoded call — Dashboards/ExecuteCustomPagedScript,
#                        Orders/ChangeOrderTag, Orders/ChangeStatus
requests.post(
    f"{server}/api/Orders/ChangeStatus",
    headers={"Authorization": session_token},          # no Content-Type — let requests set it
    data={
        "orderIds": json.dumps([pk_order_id]),         # JSON-string-of-array, then URL-encoded
        "status":   "1",
    },
)

# GET with query params (rare on Linnworks)
requests.get(
    f"{server}/api/Some/Endpoint",
    headers={"Authorization": session_token},
    params={"key": "value"},
)
```

### The 3-step "create + unpark + mark paid" recipe in one block

```python
import json

# Step 1 — create
resp = call("Orders/CreateOrders", json_body={"orders": [order_payload]})
pk_order_id = resp[0]      # bare array of strings

# Step 2 — unpark (form-encoded)
call("Orders/ChangeOrderTag",
     form_body={"orderIds": json.dumps([pk_order_id])})

# Step 3 — mark paid (form-encoded)
call("Orders/ChangeStatus",
     form_body={"orderIds": json.dumps([pk_order_id]), "status": "1"})
```

---

## Appendix B — Documentation links

| Resource | URL |
|---|---|
| Linnworks API documentation | <https://apidocs.linnworks.net/> |
| Developer dashboard | <https://developer.linnworks.com/> |
| Authentication overview | <https://apps.linnworks.net/Authorization> |
| `Orders/CreateOrders` reference | <https://help.linnworks.com/support/solutions/articles/7000013635> |
| Query Data script catalogue | <https://help.linnworks.com/support/solutions/articles/7000018696> |

The official docs are sometimes out of date or describe shapes that don't match the running API. When in doubt, **probe** — see §15.

---

## Appendix C — Things to update in this doc

This file is a working reference; keep it honest. When you discover a new gotcha or confirm an unverified shape, update the relevant section. Particularly worth re-validating periodically:

- §6 `Orders/SearchOrders` body shape (varies by API version).
- §13 `Orders/SetOrderShippingInfo` body shape (probe candidates).
- §12 the script catalogue per tenant.
- §16 list of tenant settings that affect API behaviour invisibly.
