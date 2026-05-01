"""probes/probe_linnworks_create_orders.py — Phase 0b probe 3, v2.

Replaces the v1 script which probed the wrong endpoint. The first run
of v1 returned HTTP 400 "The request is invalid." on every body shape
because of two issues we discovered after the fact:

1. **Wrong endpoint.** `Orders/CreateNewOrder` creates an empty draft
   order — Linnworks' own docs direct readers to `Orders/CreateOrders`
   (plural) for fully-formed orders with line items inline. CreateOrders
   is what we need for one-shot Square sale → Linnworks order conversion.

2. **Missing mandatory fields.** Per
   https://help.linnworks.com/support/solutions/articles/7000013635 ,
   CreateOrders requires Source, SubSource, ReferenceNumber,
   ReceivedDate, DispatchBy, OrderItems (with SKU/Qty/PricePerUnit/
   ItemTitle), and DeliveryAddress (named exactly that, not
   ShippingAddress). v1 sent none of these.

This probe sends one fully-formed order with all documented mandatory
fields and the test-data markers below. It tries the JSON wire
format first (since lib/linnworks.py natively handles it), then falls
back to form-encoded `orders=<json string of array>` matching the
help-article example.

**Every request and response is logged in full** (no truncation) so
when an attempt fails we can read what Linnworks actually said —
the v1 truncation to 300 chars was a key reason the first run was
opaque.

Test markers for orphan recovery if cleanup fails:
- ReferenceNumber       → __PROBE_TEST_{utc-timestamp}__
- DeliveryAddress.FullName → [PROBE-CLEANUP-FAILED-{utc-timestamp}]
- OrderItems[0].SKU     → __PROBE_TEST_DO_NOT_USE__
- SubSource             → PROBE_TEST

The v1 script `probes/probe_linnworks_create_order.py` is kept in the
repo as a deprecation stub so commit-history readers can see what
changed and why.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from lib import linnworks


PROBE_SKU_MARKER = "__PROBE_TEST_DO_NOT_USE__"

# Hardcoded per the v1 run's location discovery — the Default warehouse
# on this tenant uses the all-zeros UUID. _list_locations() still logs
# the full set every run so we'd notice if this ever changes.
DEFAULT_LOCATION_ID = "00000000-0000-0000-0000-000000000000"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _two_days_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()


def _customer_marker(timestamp: str) -> str:
    return f"[PROBE-CLEANUP-FAILED-{timestamp}]"


def _try_call(
    path: str,
    *,
    method: str = "POST",
    body: Optional[dict[str, Any]] = None,
    form_body: Optional[dict[str, Any]] = None,
    label: str = "",
) -> tuple[Optional[Any], int, str]:
    """Make a single Linnworks call. Return (parsed_json_or_None, status, raw_text).

    Prints the FULL request body and FULL response body — no truncation.
    The truncation in v1 made errors unreadable, which was a big part
    of why the first run was uninterpretable.
    """
    print(f"\n--- attempt: {label} ---")
    print(f"    {method} /api/{path}")
    if body is not None:
        print(f"    JSON body: {json.dumps(body)}")
    if form_body is not None:
        # Form-body keys are strings; values are usually JSON-strings.
        # Print verbatim so the wire format is visible.
        print(f"    Form body: {form_body}")
    try:
        result = linnworks.call(
            path,
            method=method,
            json_body=body,
            form_body=form_body,
        )
    except requests.HTTPError as e:
        resp = e.response
        status = resp.status_code if resp is not None else 0
        text = resp.text if resp is not None else str(e)
        print(f"    HTTP {status} — full response body:")
        print(f"    {text}")
        return (None, status, text)
    except Exception as e:
        print(f"    REQUEST FAILED: {type(e).__name__}: {e}")
        return (None, 0, str(e))
    print(f"    HTTP 200 — full response body:")
    print(f"    {json.dumps(result, default=str, indent=2)}")
    return (result, 200, "")


def _list_locations() -> None:
    """Log every stock location on the tenant.

    Verifies DEFAULT_LOCATION_ID is among them. Doesn't return anything
    — DEFAULT_LOCATION_ID is hardcoded; the listing exists for
    visibility so we'd notice if the tenant's locations ever changed.
    """
    locations, _, _ = _try_call(
        "Inventory/GetStockLocations",
        body=None,
        label="list stock locations",
    )
    if not isinstance(locations, list):
        # Some tenants name the endpoint differently.
        locations, _, _ = _try_call(
            "Stock/GetStockLocations",
            body=None,
            label="list stock locations (fallback path)",
        )
    if not isinstance(locations, list):
        print("=== DISCOVERY: could not list stock locations — proceeding with hardcoded default ===")
        return
    print(f"\n=== DISCOVERY: {len(locations)} stock location(s) on tenant ===")
    found_default = False
    for loc in locations:
        loc_id = loc.get("StockLocationId")
        marker = "  ← will use this (Default)" if loc_id == DEFAULT_LOCATION_ID else ""
        print(
            f"    • {loc.get('LocationName')!r}  "
            f"id={loc_id}  "
            f"fulfilment_centre={loc.get('IsFulfillmentCenter')}{marker}"
        )
        if loc_id == DEFAULT_LOCATION_ID:
            found_default = True
    if not found_default:
        print(
            f"=== DISCOVERY: WARNING — DEFAULT_LOCATION_ID ({DEFAULT_LOCATION_ID}) "
            "is not among this tenant's locations. CreateOrders may reject the request. ==="
        )


def _build_order(timestamp: str) -> dict[str, Any]:
    """One canonical order body with every documented mandatory field
    filled in. Tagged with markers so a leaked test order is findable.
    """
    customer = _customer_marker(timestamp)
    address = {
        "FullName":     customer,
        "Company":      "",
        "EmailAddress": "probe@example.invalid",
        "PhoneNumber":  "0000000000",
        "Address1":     "Probe",
        "Address2":     "",
        "Address3":     "",
        "Town":         "Probe",
        "Region":       "",
        "PostCode":     "PR0 BE1",
        "Country":      "United Kingdom",
        "CountryCode":  "GB",
    }
    return {
        "Source":          "DIRECT",
        "SubSource":       "PROBE_TEST",
        "ReferenceNumber": f"__PROBE_TEST_{timestamp}__",
        "ExternalReferenceNumber": f"__PROBE_TEST_{timestamp}__",
        "ReceivedDate":    _now_iso(),
        "DispatchBy":      _two_days_iso(),
        "LocationId":      DEFAULT_LOCATION_ID,
        "Currency":        "GBP",
        "OrderItems": [
            {
                "SKU":          PROBE_SKU_MARKER,
                "ChannelSKU":   PROBE_SKU_MARKER,
                "ItemTitle":    "Probe test (do not process)",
                "ItemNumber":   PROBE_SKU_MARKER,
                "Qty":          1,
                "PricePerUnit": 0.01,
                "Discount":     0,
                "LineDiscount": 0,
                "TaxRate":      0,
            }
        ],
        "DeliveryAddress": address,
        "BillingAddress":  address,
    }


def _extract_pk_order_id(result: Any) -> Optional[str]:
    """Find the new order id in the response.

    Orders/CreateOrders is documented to return an array of created
    orders. Other shapes (single object, wrapped under "Orders" or
    "Data") are tried defensively in case the tenant's version differs.
    """
    candidates: list[Any] = []
    if isinstance(result, list):
        candidates = result
    elif isinstance(result, dict):
        for key in ("Orders", "Data", "result"):
            inner = result.get(key)
            if isinstance(inner, list):
                candidates = inner
                break
        if not candidates:
            candidates = [result]
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        for field in ("OrderId", "pkOrderID", "pkOrderId", "Id", "OrderID"):
            if cand.get(field):
                return str(cand[field])
        general = cand.get("GeneralInfo")
        if isinstance(general, dict):
            for field in ("OrderId", "pkOrderID", "pkOrderId"):
                if general.get(field):
                    return str(general[field])
    return None


def _attempt_cleanup(pk_order_id: str) -> bool:
    """Try Orders/DeleteOrder (singular, per the help-article hint),
    fall back to plural and CancelOrder.
    """
    candidates = [
        ("Orders/DeleteOrder",  {"orderId": pk_order_id},      "DeleteOrder, single orderId"),
        ("Orders/DeleteOrder",  {"pkOrderId": pk_order_id},    "DeleteOrder, pkOrderId variant"),
        ("Orders/DeleteOrders", {"orderIds": [pk_order_id]},   "DeleteOrders, orderIds array"),
        ("Orders/DeleteOrders", {"pkOrderIds": [pk_order_id]}, "DeleteOrders, pkOrderIds array"),
        ("Orders/CancelOrder",  {"orderId": pk_order_id},      "CancelOrder fallback"),
    ]
    for path, body, label in candidates:
        result, status, err = _try_call(path, body=body, label=f"cleanup → {label}")
        if status == 200:
            print(f"=== DISCOVERY: cleanup endpoint that worked: {path} body={json.dumps(body)} ===")
            return True
    return False


def create_test_order_via_create_orders(timestamp: str) -> tuple[Optional[str], Optional[str]]:
    """Try the documented wire formats for Orders/CreateOrders.

    Returns (pk_order_id, label_of_attempt_that_worked). Either may
    be None if no shape worked. Imported by probe 4 (mark-paid) so it
    can create its own test order using the same locked-in shape.
    """
    order = _build_order(timestamp)

    # Multiple attempts kept on purpose — they're a record of what was
    # tried, per the prompt's "do not delete alternative-shape candidates"
    # instruction. The first that returns a usable id wins.
    attempts: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = [
        # 1. JSON, wrapped — the most-likely-correct shape.
        ("CreateOrders — JSON, {orders:[order]}", {"orders": [order]}, None),
        # 2. JSON, single object — some Linnworks endpoints accept this.
        ("CreateOrders — JSON, {orders:order}",   {"orders":  order},   None),
        # 3. Form-encoded, JSON-string array — matches the help-article example.
        ("CreateOrders — form, orders=<json array>",  None, {"orders": json.dumps([order])}),
        # 4. Form-encoded, JSON-string single — last resort.
        ("CreateOrders — form, orders=<json single>", None, {"orders": json.dumps(order)}),
    ]
    for label, json_body, form_body in attempts:
        result, status, err = _try_call(
            "Orders/CreateOrders",
            body=json_body,
            form_body=form_body,
            label=label,
        )
        if status != 200 or result is None:
            continue
        pk = _extract_pk_order_id(result)
        if pk:
            return (pk, label)
        print(f"    HTTP 200 but no recognisable order id in response — trying next wire format")
    return (None, None)


def main() -> int:
    print("--- probe_linnworks_create_orders (Orders/CreateOrders v2) ---")

    _list_locations()

    timestamp = _utc_timestamp()
    pk_order_id: Optional[str] = None
    working_attempt: Optional[str] = None

    try:
        pk_order_id, working_attempt = create_test_order_via_create_orders(timestamp)

        if pk_order_id:
            print(
                f"\n=== DISCOVERY: Orders/CreateOrders working wire format: {working_attempt} ===\n"
                f"=== DISCOVERY: working order body (mandatory fields) = "
                f"{json.dumps(_build_order(timestamp))} ===\n"
                f"=== DISCOVERY: new pkOrderID = {pk_order_id} ===\n"
            )
            print(f"!!! TEST ORDER pkOrderID = {pk_order_id} — manual cleanup id if needed !!!")
        else:
            print(
                "\n=== DISCOVERY: NO Orders/CreateOrders wire format returned a usable order id. "
                "Read the FULL error bodies above for hints from Linnworks, "
                "extend the attempts list in create_test_order_via_create_orders(), "
                "and re-run. ==="
            )
            return 2

    finally:
        if pk_order_id:
            print(f"\n--- cleanup: deleting test order {pk_order_id} ---")
            cleaned = _attempt_cleanup(pk_order_id)
            if not cleaned:
                print(
                    f"\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
                    f"!!! CLEANUP FAILED — orphan order pkOrderID:    !!!\n"
                    f"!!!   {pk_order_id}\n"
                    f"!!! Find it in Linnworks UI by searching for\n"
                    f"!!!   reference number = __PROBE_TEST_{timestamp}__\n"
                    f"!!!   or customer name '[PROBE-CLEANUP-FAILED-...'\n"
                    f"!!!   or line-item SKU '{PROBE_SKU_MARKER}'\n"
                    f"!!! and delete it manually before re-running.\n"
                    f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
                )
                return 3
            print(f"=== DISCOVERY: test order {pk_order_id} cleaned up successfully ===")

    return 0


if __name__ == "__main__":
    sys.exit(main())
