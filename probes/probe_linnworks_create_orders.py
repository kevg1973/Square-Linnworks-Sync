"""probes/probe_linnworks_create_orders.py — Phase 0b probe 3, v3.

v2 actually succeeded on attempt 1 (`{"orders": [order]}` returned
HTTP 200 with body `["<uuid>"]`) but two follow-on bugs hid it:

1. **Response parser was too strict.** It only looked for dict-shaped
   responses with a `pkOrderID`/`OrderId` key. The real Linnworks
   response is a *bare JSON array* of UUID strings — every shape
   matched zero keys and the parser said "no recognisable order id"
   on the very response that contained the id.

2. **Cleanup only ran on a successful parse.** When the parser fell
   through, the test order leaked. The probe then kept trying
   alternative wire formats — Linnworks deduplicates on
   Source+SubSource+ReferenceNumber so each retry returned the same
   pkOrderID, but if dedup hadn't kicked in we'd have created
   duplicates.

v3 fixes both. The parser now accepts:
- bare list of UUID strings              (the actual prod shape)
- list of dicts with pk/OrderId keys
- bare UUID string
- dict with pk/OrderId keys (direct or under GeneralInfo)
- wrapper dicts under "Orders"/"Data"/"result"

A `_self_test_parser()` runs at startup with the real production
shape `["98c01c1a-cdfd-46f2-9bce-4c19d268bbe0"]` and 13 other cases,
so a future regression breaks loudly in CI before any API calls.

Cleanup logic now triggers on *any* HTTP 200, not just on a
successful parse. If the parser fails, the probe falls back to
`Orders/GetOrderDetailsByReferenceId` (looking up the order by its
unique ReferenceNumber) before declaring an orphan. And the wire-
format loop **stops at the first 200** — Linnworks dedup means
re-attempts are wasted, and could create duplicates if dedup ever
fails to apply.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from lib import linnworks


PROBE_SKU_MARKER = "__PROBE_TEST_DO_NOT_USE__"

# Hardcoded per the v1 location-listing run on this tenant. The probe
# still lists the live set every run so a tenant change would be
# obvious in the workflow log.
DEFAULT_LOCATION_ID = "00000000-0000-0000-0000-000000000000"

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _two_days_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()


def _customer_marker(timestamp: str) -> str:
    return f"[PROBE-CLEANUP-FAILED-{timestamp}]"


def _looks_like_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(UUID_RE.match(value))


def _extract_pk_order_id(result: Any) -> Optional[str]:
    """Find a pkOrderID in any of the response shapes Linnworks returns.

    Locked-in shapes (see _self_test_parser):
    - bare UUID string                             "<uuid>"
    - bare list of UUID strings                    ["<uuid>", ...]   ← prod
    - list of dicts with pk fields                 [{"OrderId": "<uuid>"}, ...]
    - dict with pk field                           {"OrderId": "<uuid>"}
    - dict with GeneralInfo.OrderId                {"GeneralInfo": {"OrderId": "<uuid>"}}
    - wrapper under Orders/Data/result             {"Orders": [...]}

    Returns the first pkOrderID found, or None.
    """
    if result is None:
        return None
    if _looks_like_uuid(result):
        return result  # type: ignore[return-value]
    if isinstance(result, list):
        for item in result:
            if _looks_like_uuid(item):
                return item
            if isinstance(item, dict):
                pk = _extract_pk_from_dict(item)
                if pk:
                    return pk
        return None
    if isinstance(result, dict):
        pk = _extract_pk_from_dict(result)
        if pk:
            return pk
        for wrapper_key in ("Orders", "Data", "result"):
            inner = result.get(wrapper_key)
            if inner is not None:
                pk = _extract_pk_order_id(inner)
                if pk:
                    return pk
    return None


def _extract_pk_from_dict(d: dict[str, Any]) -> Optional[str]:
    for field in ("pkOrderID", "pkOrderId", "OrderId", "OrderID", "Id"):
        candidate = d.get(field)
        if _looks_like_uuid(candidate):
            return str(candidate)
    general = d.get("GeneralInfo")
    if isinstance(general, dict):
        for field in ("pkOrderID", "pkOrderId", "OrderId", "OrderID"):
            candidate = general.get(field)
            if _looks_like_uuid(candidate):
                return str(candidate)
    return None


def _self_test_parser() -> None:
    """Asserts the parser handles every shape we've observed or might see.

    Runs at startup so a regression breaks loudly in CI before any real
    API calls. The first case is the real production shape that v2
    fumbled — keep it pinned at index 0 as a regression beacon.
    """
    real_uuid = "98c01c1a-cdfd-46f2-9bce-4c19d268bbe0"
    cases: list[tuple[Any, Optional[str]]] = [
        ([real_uuid],                                 real_uuid),  # ← prod
        (real_uuid,                                   real_uuid),
        ([{"OrderId": real_uuid}],                    real_uuid),
        ([{"pkOrderId": real_uuid}],                  real_uuid),
        ([{"pkOrderID": real_uuid}],                  real_uuid),
        ({"OrderId": real_uuid},                      real_uuid),
        ({"pkOrderID": real_uuid},                    real_uuid),
        ({"Orders": [real_uuid]},                     real_uuid),
        ({"Data": [{"OrderId": real_uuid}]},          real_uuid),
        ({"GeneralInfo": {"OrderId": real_uuid}},     real_uuid),
        ([],                                          None),
        ({},                                          None),
        ("not-a-uuid",                                None),
        (None,                                        None),
        ([{"unrelated": "field"}],                    None),
    ]
    for input_value, expected in cases:
        actual = _extract_pk_order_id(input_value)
        assert actual == expected, (
            f"parser regression: input={input_value!r} "
            f"expected={expected!r} got={actual!r}"
        )
    print(f"=== probe self-test: parser handles {len(cases)} response shapes correctly ===")


def _describe_response_shape(result: Any) -> str:
    """One-line human description of a CreateOrders response, for the
    discovery log. Hits the prod shape ("bare JSON array of pkOrderID
    strings") explicitly because that's the v2-fumbled case and it's
    worth being unambiguous about.
    """
    if isinstance(result, list):
        if result and all(_looks_like_uuid(x) for x in result):
            sample = json.dumps(result[:1])
            return f"bare JSON array of pkOrderID strings, e.g. {sample}"
        if result and all(isinstance(x, dict) for x in result):
            return f"JSON array of objects, first object keys: {list(result[0].keys())}"
        if not result:
            return "empty JSON array"
        return f"JSON array, mixed types: {[type(x).__name__ for x in result[:3]]}"
    if isinstance(result, dict):
        return f"JSON object, top-level keys: {list(result.keys())}"
    if _looks_like_uuid(result):
        return "bare UUID string"
    if isinstance(result, str):
        return "bare string (not a UUID)"
    return type(result).__name__


def _try_call(
    path: str,
    *,
    method: str = "POST",
    body: Optional[dict[str, Any]] = None,
    form_body: Optional[dict[str, Any]] = None,
    label: str = "",
) -> tuple[Optional[Any], int, str]:
    """Make one Linnworks call. Print full request and full response —
    no truncation. v1's truncation to 300 chars hid Linnworks' actual
    error text and made every 400 opaque.
    """
    print(f"\n--- attempt: {label} ---")
    print(f"    {method} /api/{path}")
    if body is not None:
        print(f"    JSON body: {json.dumps(body)}")
    if form_body is not None:
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
    """Log every stock location on the tenant. Verifies DEFAULT_LOCATION_ID
    is among them. Visibility-only — DEFAULT_LOCATION_ID is hardcoded.
    """
    locations, _, _ = _try_call(
        "Inventory/GetStockLocations",
        body=None,
        label="list stock locations",
    )
    if not isinstance(locations, list):
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
    filled in, and tagged with markers so a leaked test order is
    findable manually.
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


def _lookup_pk_by_reference_number(reference_number: str) -> Optional[str]:
    """Fall back when the parser can't extract a pkOrderID from a 200
    response. Looks up the order by its unique ReferenceNumber via
    Orders/GetOrderDetailsByReferenceId.

    Per LINNWORKS_REFERENCE.md §4 / Linnworks docs, that endpoint
    returns up to 50 orders matching either ReferenceNum or
    SecondaryReference. Each test order uses a unique timestamped
    ReferenceNumber, so we expect exactly one match.
    """
    print(
        f"\n--- ReferenceNumber lookup: orders matching {reference_number!r} ---"
    )
    candidates = [
        ({"referenceId": reference_number}, "GetOrderDetailsByReferenceId, camel"),
        ({"ReferenceId": reference_number}, "GetOrderDetailsByReferenceId, Pascal"),
    ]
    for body, label in candidates:
        result, status, _ = _try_call(
            "Orders/GetOrderDetailsByReferenceId",
            body=body,
            label=f"reference lookup → {label}",
        )
        if status != 200 or result is None:
            continue
        pk = _extract_pk_order_id(result)
        if pk:
            return pk
    return None


def _attempt_cleanup(pk_order_id: str) -> tuple[bool, Optional[str]]:
    """Try Orders/DeleteOrder (singular) first, fall back to plural and
    CancelOrder. Returns (success, working_endpoint_path).
    """
    candidates = [
        ("Orders/DeleteOrder",  {"orderId": pk_order_id},      "DeleteOrder, single orderId"),
        ("Orders/DeleteOrder",  {"pkOrderId": pk_order_id},    "DeleteOrder, pkOrderId variant"),
        ("Orders/DeleteOrders", {"orderIds": [pk_order_id]},   "DeleteOrders, orderIds array"),
        ("Orders/DeleteOrders", {"pkOrderIds": [pk_order_id]}, "DeleteOrders, pkOrderIds array"),
        ("Orders/CancelOrder",  {"orderId": pk_order_id},      "CancelOrder fallback"),
    ]
    for path, body, label in candidates:
        result, status, _ = _try_call(path, body=body, label=f"cleanup → {label}")
        if status == 200:
            print(f"=== DISCOVERY: cleanup body shape that worked: {json.dumps(body)} ===")
            return (True, path)
    return (False, None)


def _print_orphan_banner(
    pk_order_id: Optional[str],
    reference_number: str,
    timestamp: str,
) -> None:
    """Loud, copy-pasteable instructions for manually deleting a test
    order whose automated cleanup failed.
    """
    pk_line = (
        f"!!!   pkOrderID         = {pk_order_id}\n"
        if pk_order_id else
        f"!!!   pkOrderID         = (could not be parsed from the 200 response\n"
        f"!!!                       and ReferenceNumber lookup also failed)\n"
    )
    print(
        "\n"
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
        "!!! TEST ORDER ORPHANED — clean up manually:    !!!\n"
        + pk_line +
        f"!!!   ReferenceNumber   = {reference_number}\n"
        f"!!!   Customer FullName = [PROBE-CLEANUP-FAILED-{timestamp}]\n"
        f"!!!   Line item SKU     = {PROBE_SKU_MARKER}\n"
        f"!!!   SubSource         = PROBE_TEST\n"
        "!!! Find it in Linnworks UI by any of the above\n"
        "!!! markers and delete it before re-running.\n"
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    )


def create_test_order_via_create_orders(
    timestamp: str,
) -> tuple[Optional[str], Optional[str]]:
    """Create one test order via Orders/CreateOrders.

    Returns (pkOrderID_or_None, working_attempt_label_or_None).

    Behavior:
    - Tries wire formats in order and **stops at the first HTTP 200**
      (Linnworks dedup on Source+SubSource+ReferenceNumber means
      retries are wasted, and could duplicate if dedup ever fails).
    - On 200: parses pkOrderID. If parse fails, falls back to
      Orders/GetOrderDetailsByReferenceId. Either way the caller
      gets back a usable pk to clean up — or learns the order is
      orphaned (label set, pk None) and prints the recovery banner.

    Imported by probe 4 (mark-paid) so it can create its own test
    order using the same locked-in shape and lookup fallback.
    """
    order = _build_order(timestamp)
    reference_number = order["ReferenceNumber"]

    # Multiple attempts kept on purpose — they're a record of what was
    # tried, per the prompt's "do not delete alternative-shape candidates"
    # instruction. The loop stops at the first 200.
    attempts: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = [
        ("JSON, {orders:[order]}",     {"orders": [order]}, None),
        ("JSON, {orders:order}",       {"orders":  order},  None),
        ("form, orders=<json array>",  None, {"orders": json.dumps([order])}),
        ("form, orders=<json single>", None, {"orders": json.dumps(order)}),
    ]

    for label, json_body, form_body in attempts:
        result, status, _ = _try_call(
            "Orders/CreateOrders",
            body=json_body,
            form_body=form_body,
            label=f"CreateOrders — {label}",
        )
        if status != 200:
            continue

        # Lock in this format. Don't try alternatives — even though
        # Linnworks dedups, a hypothetical non-dedup path would
        # double-create.
        print(f"\n=== DISCOVERY: Orders/CreateOrders works with shape: {label} ===")
        print(f"=== DISCOVERY: response shape: {_describe_response_shape(result)} ===")
        print(
            "=== DISCOVERY: dedup is on Source+SubSource+ReferenceNumber — "
            "same triple returns the same pkOrderID ==="
        )

        pk = _extract_pk_order_id(result)
        if pk:
            print(f"=== DISCOVERY: pkOrderID parsed directly from response: {pk} ===")
            return (pk, label)

        # 200 but the parser couldn't find it. Don't leak — look up by
        # the unique ReferenceNumber.
        print(
            "    HTTP 200 but parser could not extract pkOrderID — "
            "falling back to Orders/GetOrderDetailsByReferenceId"
        )
        pk = _lookup_pk_by_reference_number(reference_number)
        if pk:
            print(
                f"=== DISCOVERY: pkOrderID resolved via ReferenceNumber lookup: {pk} ==="
            )
            return (pk, label)

        # 200 but neither parse nor lookup found the order. Caller
        # should treat as orphan.
        print(
            "=== DISCOVERY: WARNING — ReferenceNumber lookup also returned no match. "
            "Order will be reported as orphaned. ==="
        )
        return (None, label)

    return (None, None)


def main() -> int:
    print("--- probe_linnworks_create_orders (Orders/CreateOrders v3) ---\n")

    _self_test_parser()
    _list_locations()

    timestamp = _utc_timestamp()
    reference_number = f"__PROBE_TEST_{timestamp}__"

    pk_order_id, working_attempt = create_test_order_via_create_orders(timestamp)

    if not working_attempt:
        print(
            "\n=== DISCOVERY: NO Orders/CreateOrders wire format returned HTTP 200. "
            "Read the FULL error bodies above for hints from Linnworks, extend the "
            "attempts list in create_test_order_via_create_orders(), and re-run. ==="
        )
        return 2

    if not pk_order_id:
        # Got a 200 but the order is unrecoverable. Loud orphan banner.
        _print_orphan_banner(None, reference_number, timestamp)
        return 4

    print(f"\n!!! TEST ORDER pkOrderID = {pk_order_id} — manual cleanup id if needed !!!")

    print(f"\n--- cleanup: deleting test order {pk_order_id} ---")
    cleaned, working_path = _attempt_cleanup(pk_order_id)
    if cleaned:
        print(f"=== DISCOVERY: cleanup via {working_path} succeeded ===")
        return 0

    _print_orphan_banner(pk_order_id, reference_number, timestamp)
    return 3


if __name__ == "__main__":
    sys.exit(main())
