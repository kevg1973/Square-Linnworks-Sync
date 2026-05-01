"""probes/probe_linnworks_mark_paid.py — Phase 0b probe.

Order-pull (Phase 3) creates Linnworks orders from Square POS sales.
Square already took the money at the till, so the new Linnworks order
must be marked **paid** but **not dispatched** (Kevin processes
dispatch manually after-hours).

This probe finds the mechanism. It creates a real test order via
`Orders/CreateOrders` using the canonical wire format from
`probe_linnworks_create_orders.py` (probe 3 v2), captures a baseline
of payment-related fields via `Orders/GetOrdersById`, then tries each
candidate path:

- `Orders/SetPaymentStatus` (multiple body-shape variants)
- `Orders/AddOrderPayment` (the "record a payment" path used in the
  Linnworks UI when a payment is taken outside the channel)
- `Orders/SetOrderPayment`
- `Orders/PayOrder`

After each attempt, the order is re-read and the response is
diff-printed against the baseline so we can see what fields changed.
The probe also asserts the order **didn't** flip to a dispatched /
processed state — that would be a different (and unwanted) path.

Cleanup: the test order is deleted in a finally block via the same
logic as `probe_linnworks_create_orders.py`. If cleanup fails the
orphan pkOrderID is printed loudly with all the marker fields you can
search by.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from lib import linnworks
from probes.probe_linnworks_create_orders import (
    PROBE_SKU_MARKER,
    _attempt_cleanup,
    _try_call,
    _utc_timestamp,
    create_test_order_via_create_orders,
    _list_locations,
)


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
        # Some tenants wrap the response in {"Orders": [...]}.
        if isinstance(result, dict) and isinstance(result.get("Orders"), list):
            return result["Orders"][0] if result["Orders"] else None
        return None
    return result[0] if isinstance(result[0], dict) else None


def _payment_snapshot(order: dict[str, Any]) -> dict[str, Any]:
    """Pull just the fields plausibly related to payment / dispatch
    state out of a hydrated order, so we can diff before/after each
    mark-paid attempt without drowning in unrelated noise.
    """
    if not order:
        return {}
    general = order.get("GeneralInfo", {}) if isinstance(order.get("GeneralInfo"), dict) else {}
    totals = order.get("TotalsInfo", {}) if isinstance(order.get("TotalsInfo"), dict) else {}
    snapshot: dict[str, Any] = {}
    for key in (
        "Status", "SubStatus", "IsPaid", "Paid", "PaymentStatus",
        "ReceivedDate", "Processed", "DispatchedDate",
    ):
        if key in general:
            snapshot[f"GeneralInfo.{key}"] = general[key]
        if key in order:
            snapshot[key] = order[key]
    for key in (
        "TotalCharge", "TotalPaid", "TotalDiscount", "Currency",
        "PaymentMethod", "PaymentMethodId",
    ):
        if key in totals:
            snapshot[f"TotalsInfo.{key}"] = totals[key]
        if key in order:
            snapshot[key] = order[key]
    return snapshot


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    keys = set(before) | set(after)
    return {
        k: (before.get(k), after.get(k))
        for k in sorted(keys)
        if before.get(k) != after.get(k)
    }


def _create_test_order() -> tuple[Optional[str], str]:
    """Use probe 3 v2's canonical Orders/CreateOrders wire format to
    create one test order. Returns (pkOrderID, timestamp_used) — the
    timestamp is needed for the orphan-recovery banner so the manual
    search markers match what's on the order.
    """
    timestamp = _utc_timestamp()
    pk, working_attempt = create_test_order_via_create_orders(timestamp)
    if pk:
        print(
            f"=== DISCOVERY: test order created via {working_attempt!r}, pkOrderID = {pk} ==="
        )
        print(f"!!! TEST ORDER pkOrderID = {pk} — manual cleanup id if needed !!!")
    return (pk, timestamp)


def _candidate_mark_paid_calls(pk_order_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Each tuple is (label, path, body). Ordered most-likely-first.

    `Orders/SetPaymentStatus` and `Orders/AddOrderPayment` are the two
    Linnworks endpoints most commonly used for this. We try multiple
    body shapes per endpoint because Linnworks input shapes are
    inconsistent across tenants.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    return [
        (
            "SetPaymentStatus, flat camel, isPaid=true",
            "Orders/SetPaymentStatus",
            {"orderId": pk_order_id, "isPaid": True},
        ),
        (
            "SetPaymentStatus, flat Pascal",
            "Orders/SetPaymentStatus",
            {"OrderId": pk_order_id, "IsPaid": True},
        ),
        (
            "SetPaymentStatus, request-wrapped",
            "Orders/SetPaymentStatus",
            {"request": {"orderId": pk_order_id, "isPaid": True}},
        ),
        (
            "AddOrderPayment, single-order, GBP",
            "Orders/AddOrderPayment",
            {
                "orderId": pk_order_id,
                "transactionExternalReference": f"PROBE-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                "amount": 0.01,
                "currency": "GBP",
                "transactionFullyPaid": True,
                "paymentDate": now_iso,
            },
        ),
        (
            "SetOrderPayment, flat",
            "Orders/SetOrderPayment",
            {"orderId": pk_order_id, "isPaid": True},
        ),
        (
            "PayOrder, single orderId",
            "Orders/PayOrder",
            {"orderId": pk_order_id},
        ),
    ]


def _looks_paid(snapshot: dict[str, Any]) -> bool:
    """Heuristic — 'paid' state per the snapshot fields. Returns True
    if any of the typical paid-indicator fields are set.
    """
    if snapshot.get("GeneralInfo.IsPaid") is True:
        return True
    if snapshot.get("IsPaid") is True:
        return True
    if snapshot.get("Paid") is True:
        return True
    payment_status = snapshot.get("GeneralInfo.PaymentStatus") or snapshot.get("PaymentStatus")
    if isinstance(payment_status, str) and payment_status.lower() in {"paid", "fullypaid", "completed"}:
        return True
    total_charge = snapshot.get("TotalsInfo.TotalCharge") or snapshot.get("TotalCharge")
    total_paid = snapshot.get("TotalsInfo.TotalPaid") or snapshot.get("TotalPaid")
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
    status = snapshot.get("GeneralInfo.Status") or snapshot.get("Status")
    if isinstance(status, (int, str)) and str(status).lower() in {"processed", "dispatched", "1"}:
        return True
    return False


def main() -> int:
    print("--- probe_linnworks_mark_paid ---")

    # Visibility only — DEFAULT_LOCATION_ID is hardcoded in the
    # CreateOrders helper. We log the live list so a tenant change
    # would be obvious in the run log.
    _list_locations()

    pk_order_id: Optional[str] = None
    timestamp: str = ""
    working_label: Optional[str] = None
    working_path: Optional[str] = None
    working_body: Optional[dict[str, Any]] = None
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
        print(f"\n=== DISCOVERY: baseline payment fields on new order ===")
        print(json.dumps(baseline, indent=2, default=str))

        for label, path, body in _candidate_mark_paid_calls(pk_order_id):
            result, status, err = _try_call(path, body=body, label=f"mark-paid → {label}")
            if status != 200:
                continue
            after_order = _read_order(pk_order_id)
            if not after_order:
                print(f"    HTTP 200 from {path} but readback failed — skipping verification")
                continue
            after = _payment_snapshot(after_order)
            changes = _diff(baseline, after)
            print(f"    fields that changed since baseline: {json.dumps(changes, default=str)}")

            if _looks_dispatched(after):
                print(
                    f"=== DISCOVERY: WARNING — {path} ({label}) appears to have dispatched the order. "
                    "This is the wrong path for our use case. ==="
                )
                final_dispatched = True
                # Don't break — keep trying other candidates so the probe
                # output is complete. But flag loudly.
                continue

            if _looks_paid(after) and not _looks_dispatched(after):
                working_label = label
                working_path = path
                working_body = body
                print(
                    f"\n=== DISCOVERY: mark-paid endpoint that worked: {path} ===\n"
                    f"=== DISCOVERY: working body = {json.dumps(body)} ===\n"
                    f"=== DISCOVERY: post-call payment snapshot = {json.dumps(after, default=str)} ===\n"
                    f"=== DISCOVERY: order is paid AND not dispatched — correct path. ===\n"
                )
                break
            else:
                print(f"    {path} returned 200 but order does not look paid yet — trying next candidate")

        if not working_path:
            print(
                "\n=== DISCOVERY: NO candidate marked the order as paid without dispatching. "
                "Read the changes diffs above for hints, extend _candidate_mark_paid_calls(), re-run. ==="
            )

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
                return 4
            print(f"=== DISCOVERY: test order {pk_order_id} cleaned up successfully ===")

    if final_dispatched and not working_path:
        return 5
    if not working_path:
        return 6
    print(f"\n=== DISCOVERY: probe complete. Working mark-paid: {working_path} ({working_label!r}) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
