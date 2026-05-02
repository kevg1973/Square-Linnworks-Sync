"""probes/probe_linnworks_mark_paid.py — Phase 0b probe (v4).

Order-pull (Phase 3) creates Linnworks orders from Square POS sales.
Square already took the money at the till, so the new Linnworks order
must be marked **paid** but **not dispatched** (Kevin processes
dispatch manually after-hours).

## What the previous attempts taught us

**v1** fired six request shapes across four endpoint paths —
`Orders/SetPaymentStatus`, `Orders/AddOrderPayment`,
`Orders/SetOrderPayment`, `Orders/PayOrder`. **All four returned
HTTP 404.** Those names don't exist; never re-test them.

**v2** identified `Orders/ChangeStatus` (status enum 1=PAID) as the
documented mark-as-paid endpoint and tried it as JSON. The endpoint
returned HTTP 200 but the order's `GeneralInfo.Status` did not flip,
which looked like the call was being silently ignored.

**v3** switched ChangeStatus to form-encoded (correct, matches UI
capture) but used `Orders/SetOrderParkedStatus` for the unpark step
— that endpoint doesn't exist on this tenant and 404'd.

## What v4 does — both endpoints captured from UI traffic

Kevin captured the actual HTTP request the Linnworks dashboard fires
when a user clicks **Actions → Change status → Paid** in the UI:

- **Endpoint**: `POST /api/Orders/ChangeStatus`
- **Encoding**: `application/x-www-form-urlencoded`, **not JSON**.
  v2's JSON request returned 200 but did nothing — Linnworks' router
  silently no-ops unrecognised body shapes on this endpoint.
- **Form fields**:
    - `orderIds` = literal string `["<uuid>"]` — i.e. a JSON-encoded
      array of UUIDs serialised into the form value, then URL-encoded
      by the HTTP client. Same trick as
      `Dashboards/ExecuteCustomPagedScript`'s `parameters` field per
      LINNWORKS_REFERENCE.md §6.
    - `status` = `1` (the enum: `0` = Unpaid, `1` = Paid, confirmed
      against the UI).

Orders created via `Orders/CreateOrders` with `Source = "DIRECT"`
land **parked** (`IsParked: true` per probe 3). On a parked order
`ChangeStatus` is silently ignored. So v4 is structured as a
two-step recipe:

1. **Unpark** via `Orders/ChangeOrderTag`, form-encoded
   `orderIds=["<uuid>"]` (no other fields — the endpoint name itself
   implies the unpark action). Endpoint and body captured from
   Linnworks dashboard DevTools 2026-05-02. Verify by readback that
   `IsParked` flipped to `false`.
2. **Mark paid** via `Orders/ChangeStatus`, form-encoded
   `orderIds=["<uuid>"]&status=1`. Verify by readback that
   `GeneralInfo.Status` flipped to `1` AND `Processed` stayed
   `false` (we want paid, not dispatched).

If both verifications pass, the recipe is locked in and DISCOVERIES.md
§4 gets updated.

## Cleanup

The test order is deleted in a finally block via the same logic as
`probe_linnworks_create_orders.py`. If cleanup fails the orphan
pkOrderID is printed loudly with all the marker fields you can
search by.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from probes.probe_linnworks_create_orders import (
    PROBE_SKU_MARKER,
    _attempt_cleanup,
    _try_call,
    _utc_timestamp,
    create_test_order_via_create_orders,
    _list_locations,
)


# ---------- order readback / snapshot helpers ----------


def _read_order(pk_order_id: str) -> Optional[dict[str, Any]]:
    """Hydrate the order via `Orders/GetOrdersById` and return the
    single-order dict. Returns None on read failure.
    """
    result, status, _ = _try_call(
        "Orders/GetOrdersById",
        body={"pkOrderIds": [pk_order_id]},
        label=f"read back order {pk_order_id[:8]}…",
    )
    if status != 200 or not isinstance(result, list) or not result:
        if isinstance(result, dict) and isinstance(result.get("Orders"), list):
            return result["Orders"][0] if result["Orders"] else None
        return None
    return result[0] if isinstance(result[0], dict) else None


def _payment_snapshot(order: dict[str, Any]) -> dict[str, Any]:
    """Pull just the fields plausibly related to payment / parked /
    dispatch state out of a hydrated order, so we can diff before/after
    each attempt without drowning in unrelated noise.
    """
    if not order:
        return {}
    general = order.get("GeneralInfo", {}) if isinstance(order.get("GeneralInfo"), dict) else {}
    totals = order.get("TotalsInfo", {}) if isinstance(order.get("TotalsInfo"), dict) else {}
    snapshot: dict[str, Any] = {}

    for key in ("IsParked", "Processed", "DispatchedDate"):
        if key in order:
            snapshot[key] = order[key]

    for key in (
        "Status", "SubStatus", "IsPaid", "Paid", "PaymentStatus",
        "ReceivedDate", "Processed", "DispatchedDate",
    ):
        if key in general:
            snapshot[f"GeneralInfo.{key}"] = general[key]

    for key in (
        "TotalCharge", "TotalPaid", "TotalDiscount", "Currency",
        "PaymentMethod", "PaymentMethodId",
    ):
        if key in totals:
            snapshot[f"TotalsInfo.{key}"] = totals[key]

    return snapshot


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    keys = set(before) | set(after)
    return {
        k: (before.get(k), after.get(k))
        for k in sorted(keys)
        if before.get(k) != after.get(k)
    }


# ---------- the two targeted form-encoded calls ----------


def _form_orderids(pk_order_id: str) -> str:
    """Form-field value for `orderIds`: a JSON-encoded array as a
    string. The brackets and quotes ARE part of the value; requests
    URL-encodes the whole thing on the wire. Same shape as the
    Linnworks dashboard sends.
    """
    return json.dumps([pk_order_id])


def _attempt_unpark(pk_order_id: str) -> int:
    """POST Orders/ChangeOrderTag form-encoded. Returns HTTP status.

    Endpoint and body shape captured from Linnworks dashboard DevTools
    on 2026-05-02. Body is just orderIds — the endpoint name itself
    implies the unpark action; no other fields.
    """
    form = {
        "orderIds": _form_orderids(pk_order_id),
    }
    _, status, _ = _try_call(
        "Orders/ChangeOrderTag",
        form_body=form,
        label="unpark via Orders/ChangeOrderTag (form-encoded)",
    )
    return status


def _attempt_change_status_paid(pk_order_id: str) -> int:
    """POST Orders/ChangeStatus form-encoded with status=1 (Paid).
    Returns HTTP status.
    """
    form = {
        "orderIds": _form_orderids(pk_order_id),
        "status": "1",
    }
    _, status, _ = _try_call(
        "Orders/ChangeStatus",
        form_body=form,
        label="mark paid via Orders/ChangeStatus (form-encoded, status=1)",
    )
    return status


def _verify(pk_order_id: str, baseline: dict[str, Any], stage_label: str) -> Optional[dict[str, Any]]:
    after_order = _read_order(pk_order_id)
    if not after_order:
        print(f"    [{stage_label}] readback failed — cannot verify")
        return None
    after = _payment_snapshot(after_order)
    changes = _diff(baseline, after)
    print(f"    [{stage_label}] fields that changed since baseline: {json.dumps(changes, default=str)}")
    return after


# ---------- main flow ----------


def _create_test_order() -> tuple[Optional[str], str]:
    timestamp = _utc_timestamp()
    pk, working_attempt = create_test_order_via_create_orders(timestamp)
    if pk:
        print(
            f"=== DISCOVERY: test order created via {working_attempt!r}, pkOrderID = {pk} ==="
        )
        print(f"!!! TEST ORDER pkOrderID = {pk} — manual cleanup id if needed !!!")
    return (pk, timestamp)


def main() -> int:
    print("--- probe_linnworks_mark_paid (v4 — unpark via ChangeOrderTag, mark paid via ChangeStatus) ---")

    _list_locations()

    pk_order_id: Optional[str] = None
    timestamp: str = ""
    recipe_works = False

    try:
        pk_order_id, timestamp = _create_test_order()
        if not pk_order_id:
            print(
                "=== DISCOVERY: could not create a test order via Orders/CreateOrders. "
                "Run probe 3 first to lock in the working wire format, then re-run. ==="
            )
            return 2

        baseline_order = _read_order(pk_order_id)
        if not baseline_order:
            print(
                "=== DISCOVERY: created order but Orders/GetOrdersById didn't return it. "
                "Cannot diff — aborting after cleanup. ==="
            )
            return 3
        baseline = _payment_snapshot(baseline_order)
        print(f"\n=== DISCOVERY: baseline payment + parked fields on new order ===")
        print(json.dumps(baseline, indent=2, default=str))

        # ---------- Step 1 — unpark ----------
        print("\n--- step 1: unpark via Orders/ChangeOrderTag (form-encoded) ---")
        unpark_status = _attempt_unpark(pk_order_id)
        if unpark_status == 404:
            print(
                "=== DISCOVERY: Orders/ChangeOrderTag returned HTTP 404. "
                "Unexpected — this endpoint was captured directly from the Linnworks "
                "dashboard. Re-capture from DevTools and confirm the path. ==="
            )
            return 7
        if unpark_status != 200:
            print(
                f"=== DISCOVERY: Orders/ChangeOrderTag returned HTTP {unpark_status}. "
                "Body shape may have drifted from the UI capture. Re-capture from DevTools. ==="
            )
            return 8

        after_unpark = _verify(pk_order_id, baseline, "after step 1 (unpark)")
        if after_unpark is None or after_unpark.get("IsParked") is not False:
            print(
                "=== DISCOVERY: ChangeOrderTag returned 200 but IsParked did not flip "
                "to false. Re-capture the unpark request from the UI to confirm body. ==="
            )
            return 9
        print("=== DISCOVERY: step 1 OK — order is now unparked (IsParked: false) ===")

        # ---------- Step 2 — mark paid ----------
        print("\n--- step 2: mark paid via Orders/ChangeStatus (form-encoded, status=1) ---")
        paid_status = _attempt_change_status_paid(pk_order_id)
        if paid_status != 200:
            print(
                f"=== DISCOVERY: Orders/ChangeStatus returned HTTP {paid_status} after a "
                "successful unpark. Unexpected — the UI capture says this call works. "
                "Inspect the response body above. ==="
            )
            return 10

        after_paid = _verify(pk_order_id, baseline, "after step 2 (mark paid)")
        if after_paid is None:
            return 11

        status_now = after_paid.get("GeneralInfo.Status")
        processed_now = after_paid.get("Processed", after_paid.get("GeneralInfo.Processed"))
        if status_now == 1 and processed_now is not True:
            recipe_works = True
            print(
                "\n=== DISCOVERY: mark-paid recipe CONFIRMED ===\n"
                "=== DISCOVERY: step 1 (unpark)   = POST /api/Orders/ChangeOrderTag, form-encoded, body=orderIds=[\"<uuid>\"] ===\n"
                "=== DISCOVERY: step 2 (mark paid) = POST /api/Orders/ChangeStatus,   form-encoded, body=orderIds=[\"<uuid>\"]&status=1 ===\n"
                "=== DISCOVERY: status enum = 0=Unpaid, 1=Paid (confirmed via UI capture 2026-05-02) ===\n"
                "=== DISCOVERY: parked orders silently no-op ChangeStatus — ChangeOrderTag MUST run first ===\n"
                f"=== DISCOVERY: post-call snapshot = {json.dumps(after_paid, default=str)} ===\n"
            )
        else:
            print(
                f"=== DISCOVERY: ChangeStatus returned 200 but verification failed. "
                f"GeneralInfo.Status={status_now!r} (want 1), Processed={processed_now!r} "
                "(want false). Read the diff above. ==="
            )

    finally:
        if pk_order_id:
            print(f"\n--- cleanup: deleting test order {pk_order_id} ---")
            cleaned, cleanup_path = _attempt_cleanup(pk_order_id)
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
                return 4
            print(f"=== DISCOVERY: cleanup via {cleanup_path} succeeded ===")

    if not recipe_works:
        return 6
    print(
        "\n=== DISCOVERY: probe complete. Recipe: "
        "(1) Orders/ChangeOrderTag form-encoded {orderIds:'[\"<uuid>\"]'} "
        "(2) Orders/ChangeStatus form-encoded {orderIds:'[\"<uuid>\"]', status:'1'} ==="
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
