"""probes/probe_linnworks_create_order.py — Phase 0b probe.

Probes the body shape that `Orders/CreateNewOrder` accepts on the
Northwest Guitars Linnworks tenant. Per §10 of LINNWORKS_REFERENCE.md,
this endpoint's body shape is tenant-dependent and the docs are
stale.

The probe creates a real test order, prints the new `pkOrderID`
prominently in the run log, then deletes it in a finally block.

Defensive markers on the test order so a failed cleanup can be
recovered manually:

- Customer name → `[PROBE-CLEANUP-FAILED-{utc-timestamp}]`
- Line item SKU → `__PROBE_TEST_DO_NOT_USE__`  (deliberately invalid;
  won't match any real inventory)

If the working shape doesn't accept these fields directly,
follow-up update calls are attempted on a best-effort basis and any
failures are logged but don't abort the probe — finding the
CreateNewOrder shape is the primary goal.

Cleanup endpoint candidates tried in order: `Orders/DeleteOrders`,
`Orders/CancelOrder`. If both fail, the orphan `pkOrderID` is printed
loudly so it can be deleted manually from the Linnworks UI.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from lib import linnworks


PROBE_SKU_MARKER = "__PROBE_TEST_DO_NOT_USE__"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _customer_marker() -> str:
    return f"[PROBE-CLEANUP-FAILED-{_utc_timestamp()}]"


def _try_call(
    path: str,
    *,
    method: str = "POST",
    body: Optional[dict[str, Any]] = None,
    label: str = "",
) -> tuple[Optional[Any], Optional[int], str]:
    """Make a single Linnworks call and return (parsed_json, status, error_text).

    Wraps `linnworks.call` so a non-2xx response doesn't abort the
    probe — instead it yields (None, status_code, error_body) so the
    caller can decide whether to keep trying alternative shapes.
    """
    print(f"\n--- attempt: {label} ---")
    print(f"    {method} {path}")
    if body is not None:
        print(f"    body: {json.dumps(body)[:300]}")
    try:
        result = linnworks.call(path, method=method, json_body=body)
    except requests.HTTPError as e:
        resp = e.response
        status = resp.status_code if resp is not None else 0
        text = resp.text[:500] if resp is not None else str(e)
        print(f"    HTTP {status} — {text}")
        return (None, status, text)
    except Exception as e:
        print(f"    REQUEST FAILED: {type(e).__name__}: {e}")
        return (None, 0, str(e))
    print(f"    HTTP 200 — keys: {list(result.keys()) if isinstance(result, dict) else type(result).__name__}")
    return (result, 200, "")


def _get_default_location_id() -> Optional[str]:
    """Look up the tenant's default stock location.

    Per LINNWORKS_REFERENCE.md §3, `Stock/GetStockLocations` returns
    `[{StockLocationId, LocationName, IsFulfillmentCenter, ...}]`. We
    pick the location named exactly "Default" if present, otherwise
    the first non-fulfillment-centre location, otherwise the first
    location returned.
    """
    locations, status, err = _try_call(
        "Inventory/GetStockLocations",
        body=None,
        label="get stock locations",
    )
    if locations is None or not isinstance(locations, list):
        # Some tenants name the endpoint slightly differently.
        locations, status, err = _try_call(
            "Stock/GetStockLocations",
            body=None,
            label="get stock locations (fallback path)",
        )
    if not isinstance(locations, list) or not locations:
        return None

    print(f"\n=== DISCOVERY: tenant has {len(locations)} stock location(s) ===")
    for loc in locations:
        print(f"    • {loc.get('LocationName')!r}  id={loc.get('StockLocationId')}  "
              f"fulfilment_centre={loc.get('IsFulfillmentCenter')}")

    default = next(
        (loc for loc in locations if loc.get("LocationName") == "Default"),
        None,
    )
    if default is None:
        default = next(
            (loc for loc in locations if not loc.get("IsFulfillmentCenter")),
            locations[0],
        )
    chosen = default.get("StockLocationId")
    print(f"=== DISCOVERY: probe will use location_id = {chosen} "
          f"({default.get('LocationName')!r}) ===")
    return chosen


def _extract_pk_order_id(result: Any) -> Optional[str]:
    """Find a UUID-like field in the response that plausibly is the new
    order id. Linnworks responses are inconsistent — sometimes the new
    order id is `OrderId`, sometimes `pkOrderId`, sometimes the whole
    new order is returned with a nested `OrderId` inside `GeneralInfo`.
    """
    if not isinstance(result, dict):
        return None
    for candidate in ("OrderId", "pkOrderID", "pkOrderId", "Id", "OrderID"):
        if candidate in result and result[candidate]:
            return str(result[candidate])
    general = result.get("GeneralInfo")
    if isinstance(general, dict):
        for candidate in ("OrderId", "pkOrderID", "pkOrderId"):
            if candidate in general and general[candidate]:
                return str(general[candidate])
    return None


def _attempt_cleanup(pk_order_id: str) -> bool:
    """Try Orders/DeleteOrders, then Orders/CancelOrder. Return True if
    either succeeded. Loud about failures.
    """
    candidates = [
        ("Orders/DeleteOrders", {"orderIds": [pk_order_id]}, "DeleteOrders, orderIds array"),
        ("Orders/DeleteOrders", {"pkOrderIds": [pk_order_id]}, "DeleteOrders, pkOrderIds array"),
        ("Orders/CancelOrder",  {"orderId": pk_order_id},      "CancelOrder, single orderId"),
    ]
    for path, body, label in candidates:
        result, status, err = _try_call(path, body=body, label=f"cleanup → {label}")
        if status == 200:
            print(f"=== DISCOVERY: cleanup endpoint that worked: {path} with body {body} ===")
            return True
    return False


def _candidate_create_bodies(location_id: str) -> list[tuple[str, dict[str, Any]]]:
    """The body shapes worth trying. Ordered most-likely-correct first.

    The Linnworks public docs imply `Orders/CreateNewOrder` takes
    `{locationId, currency}` and returns an empty order shell, but
    docs are stale — try variants until one returns 200 with a
    pkOrderID-like field.
    """
    customer = _customer_marker()
    return [
        (
            "flat camelCase: locationId + currency",
            {"locationId": location_id, "currency": "GBP"},
        ),
        (
            "flat PascalCase: LocationId + Currency",
            {"LocationId": location_id, "Currency": "GBP"},
        ),
        (
            "fkLocationId variant",
            {"fkLocationId": location_id, "currency": "GBP"},
        ),
        (
            "request-wrapped",
            {"request": {"locationId": location_id, "currency": "GBP"}},
        ),
        (
            "newOrder-wrapped",
            {"newOrder": {"locationId": location_id, "currency": "GBP"}},
        ),
        (
            "with customer marker (camel)",
            {
                "locationId": location_id,
                "currency": "GBP",
                "customerName": customer,
            },
        ),
        (
            "empty body",
            {},
        ),
    ]


def main() -> int:
    print("--- probe_linnworks_create_order ---")

    location_id = _get_default_location_id()
    if not location_id:
        print("=== DISCOVERY: could not determine a stock location — cannot proceed ===")
        return 1

    pk_order_id: Optional[str] = None
    working_shape: Optional[str] = None
    working_body: Optional[dict[str, Any]] = None
    working_response_keys: Optional[list[str]] = None

    try:
        for label, body in _candidate_create_bodies(location_id):
            result, status, err = _try_call(
                "Orders/CreateNewOrder",
                body=body,
                label=f"CreateNewOrder — {label}",
            )
            if status != 200 or result is None:
                continue
            candidate_id = _extract_pk_order_id(result)
            if not candidate_id:
                print(f"    HTTP 200 but no recognisable order-id field — skipping")
                continue
            pk_order_id = candidate_id
            working_shape = label
            working_body = body
            working_response_keys = (
                list(result.keys()) if isinstance(result, dict) else []
            )
            print(
                f"\n=== DISCOVERY: Orders/CreateNewOrder body shape that worked: {label} ===\n"
                f"=== DISCOVERY: working body = {json.dumps(body)} ===\n"
                f"=== DISCOVERY: response top-level keys = {working_response_keys} ===\n"
                f"=== DISCOVERY: new pkOrderID = {pk_order_id} ===\n"
            )
            print(f"!!! TEST ORDER pkOrderID = {pk_order_id} — manual cleanup id if needed !!!")
            break

        if not pk_order_id:
            print(
                "\n=== DISCOVERY: NO body shape returned a pkOrderID. "
                "All candidates failed — read the error bodies above, "
                "extend _candidate_create_bodies(), re-run. ==="
            )
            return 2

        # Best-effort: tag the order with marker fields so a failed
        # cleanup can be found in the Linnworks UI by searching for
        # __PROBE_TEST_DO_NOT_USE__ or [PROBE-CLEANUP-FAILED-...].
        # We DO NOT abort the probe if these fail — finding the
        # CreateNewOrder shape is the primary deliverable.
        marker_attempts = [
            (
                "Orders/SetOrderCustomerInfo",
                {
                    "orderId": pk_order_id,
                    "info": {"BillingAddress": {"FullName": _customer_marker()}},
                },
                "tag customer name (best effort)",
            ),
            (
                "Orders/AddOrderItem",
                {"orderId": pk_order_id, "itemId": None, "channelSKU": PROBE_SKU_MARKER},
                "tag line item SKU (best effort)",
            ),
        ]
        for path, body, label in marker_attempts:
            _try_call(path, body=body, label=label)

    finally:
        if pk_order_id:
            print(f"\n--- cleanup: deleting test order {pk_order_id} ---")
            cleaned = _attempt_cleanup(pk_order_id)
            if not cleaned:
                print(
                    f"\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
                    f"!!! CLEANUP FAILED — orphan order pkOrderID:    !!!\n"
                    f"!!!   {pk_order_id}\n"
                    f"!!! Find it in Linnworks UI by searching customer\n"
                    f"!!! name beginning '[PROBE-CLEANUP-FAILED-' and\n"
                    f"!!! delete it manually before re-running.\n"
                    f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
                )
                return 3
            print(f"=== DISCOVERY: test order {pk_order_id} cleaned up successfully ===")

    print(f"\n=== DISCOVERY: probe complete. Working shape: {working_shape!r} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
