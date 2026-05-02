"""probes/probe_linnworks_mark_paid.py — Phase 0b probe.

Order-pull (Phase 3) creates Linnworks orders from Square POS sales.
Square already took the money at the till, so the new Linnworks order
must be marked **paid** but **not dispatched** (Kevin processes
dispatch manually after-hours).

## Background — what's been ruled out

The first attempt (committed in 9a36491 / 830954d) fired six request
shapes across four endpoint paths — `Orders/SetPaymentStatus`,
`Orders/AddOrderPayment`, `Orders/SetOrderPayment`, `Orders/PayOrder`.
**All four returned HTTP 404 on this tenant.** Those names don't
exist; do not re-test them.

## What this v2 probe does

Researched the live Linnworks API reference (apidocs.linnworks.net)
to identify the canonical mark-as-paid endpoint:

- **`Orders/ChangeStatus`** is the documented endpoint, body
  `{"orderIds": [<uuid>], "status": <int>}` with the status enum
  `0=UNPAID, 1=PAID, 2=RETURN, 3=PENDING, 4=RESEND`. Source:
  https://apidocs.linnworks.net/reference/changestatus .

The complication is that `Orders/CreateOrders` lands `Source=DIRECT`
orders **parked** (`IsParked: true` per probe 3's readback). Parked
orders may reject status changes outright, so the probe is structured
as three targeted, sequential attempts:

1. **A — `Orders/ChangeStatus` (status=1)** directly. If this works on
   a parked order (Linnworks support docs hint that channel orders
   auto-unpark on payment update), we're done in one call.

2. **B — `Orders/SetOrderParkedStatus` (isParked=false)** if A didn't
   take. Endpoint exists in the URL space (apps.linnworks.net redirects
   `/Api/Method/Orders-SetOrderParkedStatus` to its readme.io ref); body
   shape inferred from the consistent Linnworks pattern of
   `{"orderIds": [<uuid>], ...}`.

3. **C — `Orders/ChangeStatus` (status=1)** retry, after B unparked
   the order.

Each attempt verifies success by reading the order back via
`Orders/GetOrdersById` and diffing the payment snapshot against the
baseline. The probe also asserts the order **didn't** flip to a
dispatched / processed state.

## Cleanup

The test order is deleted in a finally block via the same logic as
`probe_linnworks_create_orders.py`. If cleanup fails the orphan
pkOrderID is printed loudly with all the marker fields you can search
by.
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
    result, status, err = _try_call(
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

    # Top-level — IsParked lives here on this tenant per probe 3.
    for key in ("IsParked", "Processed", "DispatchedDate"):
        if key in order:
            snapshot[key] = order[key]

    # GeneralInfo — Status enum and payment-state flags
    for key in (
        "Status", "SubStatus", "IsPaid", "Paid", "PaymentStatus",
        "ReceivedDate", "Processed", "DispatchedDate",
    ):
        if key in general:
            snapshot[f"GeneralInfo.{key}"] = general[key]

    # TotalsInfo — money + payment method UUID
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


def _looks_paid(snapshot: dict[str, Any]) -> bool:
    """'paid' state per the snapshot fields. Returns True if any of the
    typical paid-indicator fields are set. Status==1 is the
    Orders/ChangeStatus enum value for PAID per
    https://apidocs.linnworks.net/reference/changestatus .
    """
    status = snapshot.get("GeneralInfo.Status")
    if status == 1 or status == "1":
        return True
    if snapshot.get("GeneralInfo.IsPaid") is True:
        return True
    if snapshot.get("IsPaid") is True:
        return True
    payment_status = snapshot.get("GeneralInfo.PaymentStatus")
    if isinstance(payment_status, str) and payment_status.lower() in {"paid", "fullypaid", "completed"}:
        return True
    total_charge = snapshot.get("TotalsInfo.TotalCharge")
    total_paid = snapshot.get("TotalsInfo.TotalPaid")
    if total_charge is not None and total_paid is not None:
        try:
            if float(total_paid) > 0 and float(total_paid) >= float(total_charge):
                return True
        except (TypeError, ValueError):
            pass
    return False


def _looks_dispatched(snapshot: dict[str, Any]) -> bool:
    """We do NOT want the order to flip to dispatched/processed."""
    if snapshot.get("Processed") is True:
        return True
    if snapshot.get("GeneralInfo.Processed") is True:
        return True
    if snapshot.get("DispatchedDate") and str(snapshot["DispatchedDate"]).startswith(("2", "1")):
        return True
    return False


def _looks_unparked(snapshot: dict[str, Any]) -> bool:
    return snapshot.get("IsParked") is False


# ---------- test order + the 3 targeted attempts ----------


def _create_test_order() -> tuple[Optional[str], str]:
    timestamp = _utc_timestamp()
    pk, working_attempt = create_test_order_via_create_orders(timestamp)
    if pk:
        print(
            f"=== DISCOVERY: test order created via {working_attempt!r}, pkOrderID = {pk} ==="
        )
        print(f"!!! TEST ORDER pkOrderID = {pk} — manual cleanup id if needed !!!")
    return (pk, timestamp)


def _attempt_change_status_paid(pk_order_id: str, label: str) -> tuple[int, dict[str, Any]]:
    """POST Orders/ChangeStatus with status=1 (PAID per the documented
    enum). Returns (http_status, body_used).
    """
    body = {"orderIds": [pk_order_id], "status": 1}
    _, status, _ = _try_call("Orders/ChangeStatus", body=body, label=label)
    return status, body


def _attempt_unpark(pk_order_id: str) -> tuple[int, dict[str, Any]]:
    """POST Orders/SetOrderParkedStatus with isParked=false. Body shape
    inferred from Linnworks's consistent `{orderIds:[uuid], <flag>}`
    pattern (matches ChangeStatus signature).
    """
    body = {"orderIds": [pk_order_id], "isParked": False}
    _, status, _ = _try_call("Orders/SetOrderParkedStatus", body=body, label="unpark via SetOrderParkedStatus")
    return status, body


def _verify(pk_order_id: str, baseline: dict[str, Any], stage_label: str) -> Optional[dict[str, Any]]:
    """Read order back, print diff vs baseline, return the new
    snapshot. None if readback failed.
    """
    after_order = _read_order(pk_order_id)
    if not after_order:
        print(f"    [{stage_label}] readback failed — cannot verify")
        return None
    after = _payment_snapshot(after_order)
    changes = _diff(baseline, after)
    print(f"    [{stage_label}] fields that changed since baseline: {json.dumps(changes, default=str)}")
    return after


def main() -> int:
    print("--- probe_linnworks_mark_paid (v2 — targeted, post-research) ---")

    # Visibility only — DEFAULT_LOCATION_ID is hardcoded in the
    # CreateOrders helper. Logging the live list keeps a tenant change
    # obvious in the workflow log.
    _list_locations()

    pk_order_id: Optional[str] = None
    timestamp: str = ""
    working_path: Optional[str] = None
    working_body: Optional[dict[str, Any]] = None
    working_recipe: Optional[str] = None  # human description of what worked
    final_dispatched = False

    try:
        pk_order_id, timestamp = _create_test_order()
        if not pk_order_id:
            print(
                "=== DISCOVERY: could not create a test order via Orders/CreateOrders. "
                "Run probe 3 (probe-linnworks-create-orders) first to lock in the "
                "working wire format, then re-run this probe. ==="
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

        # ---------- A — ChangeStatus directly on the parked order ----------
        print("\n--- attempt A: Orders/ChangeStatus(status=1) on parked order ---")
        a_status, a_body = _attempt_change_status_paid(
            pk_order_id, label="A: ChangeStatus(status=1) on parked order"
        )
        if a_status == 200:
            after_a = _verify(pk_order_id, baseline, "after A")
            if after_a is not None:
                if _looks_dispatched(after_a):
                    print(
                        "=== DISCOVERY: WARNING — A appears to have dispatched the order. "
                        "Wrong path. ==="
                    )
                    final_dispatched = True
                elif _looks_paid(after_a):
                    working_path = "Orders/ChangeStatus"
                    working_body = a_body
                    working_recipe = "Single call: Orders/ChangeStatus with status=1 on a parked order (Linnworks accepts payment transitions on parked orders directly)."
                    print(
                        f"\n=== DISCOVERY: mark-paid endpoint that worked: Orders/ChangeStatus ===\n"
                        f"=== DISCOVERY: working body = {json.dumps(a_body)} ===\n"
                        f"=== DISCOVERY: post-call payment snapshot = {json.dumps(after_a, default=str)} ===\n"
                        f"=== DISCOVERY: order is paid AND not dispatched — correct path. ===\n"
                    )

        # ---------- B + C — only if A didn't already succeed ----------
        if not working_path and not final_dispatched:
            print("\n--- attempt B: Orders/SetOrderParkedStatus(isParked=false) ---")
            b_status, b_body = _attempt_unpark(pk_order_id)
            if b_status == 200:
                after_b = _verify(pk_order_id, baseline, "after B")
                if after_b is not None and _looks_unparked(after_b):
                    print("=== DISCOVERY: unpark via Orders/SetOrderParkedStatus succeeded ===")
                    print(f"=== DISCOVERY: unpark body = {json.dumps(b_body)} ===")
                else:
                    print(
                        "    [B] HTTP 200 but IsParked did not flip to false — "
                        "body shape may be wrong. Continuing to C anyway."
                    )

                print("\n--- attempt C: Orders/ChangeStatus(status=1) retry after unpark ---")
                c_status, c_body = _attempt_change_status_paid(
                    pk_order_id, label="C: ChangeStatus(status=1) after unpark"
                )
                if c_status == 200:
                    after_c = _verify(pk_order_id, baseline, "after C")
                    if after_c is not None:
                        if _looks_dispatched(after_c):
                            print(
                                "=== DISCOVERY: WARNING — C appears to have dispatched the order. ==="
                            )
                            final_dispatched = True
                        elif _looks_paid(after_c):
                            working_path = "Orders/ChangeStatus (after unpark)"
                            working_body = c_body
                            working_recipe = (
                                "Two-step: (1) Orders/SetOrderParkedStatus with "
                                f"{json.dumps(b_body)} to unpark, then (2) "
                                f"Orders/ChangeStatus with {json.dumps(c_body)} to mark paid. "
                                "Required because parked orders reject direct status transitions."
                            )
                            print(
                                f"\n=== DISCOVERY: mark-paid two-step that worked ===\n"
                                f"=== DISCOVERY: step 1 = Orders/SetOrderParkedStatus, body = {json.dumps(b_body)} ===\n"
                                f"=== DISCOVERY: step 2 = Orders/ChangeStatus, body = {json.dumps(c_body)} ===\n"
                                f"=== DISCOVERY: post-call payment snapshot = {json.dumps(after_c, default=str)} ===\n"
                                f"=== DISCOVERY: order is paid AND not dispatched — correct path. ===\n"
                            )
            else:
                print(
                    f"    [B] HTTP {b_status} — Orders/SetOrderParkedStatus rejected this body shape. "
                    "Skipping C since we can't unpark."
                )

        if not working_path:
            print(
                "\n=== DISCOVERY: NO attempt produced a paid+not-dispatched order. "
                "Read the diffs above for hints. Likely next step: try "
                "{\"orderId\": pk, \"isParked\": false} (singular) for SetOrderParkedStatus, "
                "or look up Orders/UnlockOrder. Update DISCOVERIES.md §4 before re-probing. ==="
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

    if final_dispatched and not working_path:
        return 5
    if not working_path:
        return 6
    print(
        f"\n=== DISCOVERY: probe complete. Working mark-paid recipe: {working_recipe} ==="
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
