"""probes/probe_square_orders_recent.py — recent Square orders dump.

Fetches all COMPLETED orders from the last 7 days at the Northwest
Guitars location (L74KSP08AJ2GH) and prints everything we'd need
to convert them to Linnworks orders in Phase 3 (order-pull).

Read-only. No mutations against Square. No Supabase audit logging.

Run with:
    python -m probes.probe_square_orders_recent
"""

from __future__ import annotations

import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from lib import square


SHOP_LOCATION_ID = "L74KSP08AJ2GH"
LOOKBACK_DAYS = 7
PAGE_LIMIT = 100


# ---------- search ----------


def _search_orders(cursor: Optional[str], start_at: str, end_at: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "location_ids": [SHOP_LOCATION_ID],
        "query": {
            "filter": {
                "date_time_filter": {
                    "created_at": {
                        "start_at": start_at,
                        "end_at": end_at,
                    },
                },
                "state_filter": {
                    "states": ["COMPLETED"],
                },
            },
            "sort": {
                "sort_field": "CREATED_AT",
                "sort_order": "DESC",
            },
        },
        "limit": PAGE_LIMIT,
    }
    if cursor:
        body["cursor"] = cursor
    print(f"\n--- POST /orders/search (cursor={cursor!r}) ---")
    result = square.call("orders/search", method="POST", json_body=body)
    return result or {}


def _walk_orders(start_at: str, end_at: str) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    pages = 0

    while True:
        pages += 1
        response = _search_orders(cursor, start_at, end_at)
        page_orders = response.get("orders") or []
        orders.extend(page_orders)
        print(
            f"    HTTP 200 — {len(page_orders)} order(s) returned, "
            f"running total {len(orders)}"
        )
        cursor = response.get("cursor")
        if not cursor:
            print(f"    no cursor — last page reached after page {pages}")
            break

    return orders


# ---------- formatting helpers ----------


def _money(m: Optional[dict[str, Any]]) -> str:
    if not m:
        return "n/a"
    amount = m.get("amount")
    currency = m.get("currency") or ""
    if amount is None:
        return f"n/a (currency={currency!r})"
    return f"{amount} {currency}".strip()


def _addr_lines(addr: Optional[dict[str, Any]]) -> list[str]:
    if not addr:
        return []
    parts = [
        addr.get("address_line_1"),
        addr.get("address_line_2"),
        addr.get("address_line_3"),
        addr.get("locality"),
        addr.get("administrative_district_level_1"),
        addr.get("postal_code"),
        addr.get("country"),
    ]
    return [p for p in parts if p]


def _print_recipient(label: str, recipient: dict[str, Any], indent: str = "    ") -> None:
    print(f"{indent}{label}:")
    print(f"{indent}  display_name:  {recipient.get('display_name')!r}")
    print(f"{indent}  email_address: {recipient.get('email_address')!r}")
    print(f"{indent}  phone_number:  {recipient.get('phone_number')!r}")
    addr = recipient.get("address")
    if addr:
        print(f"{indent}  address:")
        for line in _addr_lines(addr):
            print(f"{indent}    {line}")
    else:
        print(f"{indent}  address:       (none)")


def _shipment_recipient(order: dict[str, Any]) -> Optional[dict[str, Any]]:
    fulfillments = order.get("fulfillments") or []
    if not fulfillments:
        return None
    shipment = (fulfillments[0] or {}).get("shipment_details") or {}
    return shipment.get("recipient")


def _pickup_recipient(order: dict[str, Any]) -> Optional[dict[str, Any]]:
    fulfillments = order.get("fulfillments") or []
    if not fulfillments:
        return None
    pickup = (fulfillments[0] or {}).get("pickup_details") or {}
    return pickup.get("recipient")


def _buyer_email(order: dict[str, Any]) -> Optional[str]:
    """Square sometimes exposes the buyer's email at the top level (newer
    Orders API), sometimes only on the fulfillment recipient. Check both.
    """
    top = order.get("buyer_email_address")
    if top:
        return top
    for getter in (_shipment_recipient, _pickup_recipient):
        rec = getter(order)
        if rec and rec.get("email_address"):
            return rec["email_address"]
    return None


def _has_line_item_discount(order: dict[str, Any]) -> bool:
    for li in order.get("line_items") or []:
        td = li.get("total_discount_money") or {}
        try:
            if int(td.get("amount") or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
        if li.get("applied_discounts"):
            return True
    return False


def _has_order_level_discount(order: dict[str, Any]) -> bool:
    for d in order.get("discounts") or []:
        if d.get("scope") == "ORDER":
            return True
    return False


# ---------- per-order block ----------


def _print_order(order: dict[str, Any]) -> None:
    print(f"\n  id:             {order.get('id')}")
    print(f"  state:          {order.get('state')!r}")
    print(f"  created_at:     {order.get('created_at')}")
    print(f"  updated_at:     {order.get('updated_at')}")
    print(f"  closed_at:      {order.get('closed_at')}")

    src = order.get("source") or {}
    print(f"  source.name:    {src.get('name')!r}")

    note = order.get("note")
    print(f"  note:           {note!r}" if note else "  note:           (none)")

    print(f"  total_money:                 {_money(order.get('total_money'))}")
    print(f"  total_tax_money:             {_money(order.get('total_tax_money'))}")
    print(f"  total_discount_money:        {_money(order.get('total_discount_money'))}")
    print(f"  total_service_charge_money:  {_money(order.get('total_service_charge_money'))}")

    # Tenders
    tenders = order.get("tenders") or []
    print(f"  tenders ({len(tenders)}):")
    for t_idx, tender in enumerate(tenders, start=1):
        card = tender.get("card_details") or {}
        print(
            f"    [{t_idx}] type={tender.get('type')!r}  "
            f"amount={_money(tender.get('amount_money'))}  "
            f"card_status={card.get('status')!r}"
        )

    # Customer block
    print(f"  customer_id:           {order.get('customer_id')!r}")
    print(f"  buyer_email_address:   {_buyer_email(order)!r}")

    shipment_rec = _shipment_recipient(order)
    if shipment_rec:
        _print_recipient("shipment recipient", shipment_rec, indent="  ")
    pickup_rec = _pickup_recipient(order)
    if pickup_rec:
        _print_recipient("pickup recipient", pickup_rec, indent="  ")

    # Line items
    line_items = order.get("line_items") or []
    print(f"  line_items ({len(line_items)}):")
    for li_idx, li in enumerate(line_items, start=1):
        print(f"    -- line {li_idx}/{len(line_items)} --")
        print(f"      name:                        {li.get('name')!r}")
        print(f"      variation_name:              {li.get('variation_name')!r}")
        print(f"      quantity:                    {li.get('quantity')!r}")
        print(f"      catalog_object_id:           {li.get('catalog_object_id')!r}")
        print(f"      variation_total_price_money: {_money(li.get('variation_total_price_money'))}")
        print(f"      total_money:                 {_money(li.get('total_money'))}")
        print(f"      gross_sales_money:           {_money(li.get('gross_sales_money'))}")
        print(f"      total_tax_money:             {_money(li.get('total_tax_money'))}")
        print(f"      total_discount_money:        {_money(li.get('total_discount_money'))}")
        li_note = li.get("note")
        if li_note:
            print(f"      note:                        {li_note!r}")


# ---------- main ----------


def main() -> int:
    print(f"--- probe_square_orders_recent (last {LOOKBACK_DAYS} days, COMPLETED only) ---")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)
    start_at = start.isoformat()
    end_at = end.isoformat()
    print(f"    location: {SHOP_LOCATION_ID}")
    print(f"    window:   {start_at}  →  {end_at}")

    orders = _walk_orders(start_at, end_at)

    if not orders:
        print("\n=== No COMPLETED orders in the window. ===")
        return 0

    print("\n" + "=" * 70)
    print(f"=== {len(orders)} COMPLETED order(s) in the last {LOOKBACK_DAYS} days ===")
    print("=" * 70)

    for i, order in enumerate(orders, start=1):
        print(f"\n{'-' * 70}")
        print(f"--- Order {i}/{len(orders)} ---")
        print(f"{'-' * 70}")
        _print_order(order)

    # ---------- summary ----------
    source_counter: Counter = Counter(
        ((o.get("source") or {}).get("name") or "(no source.name)") for o in orders
    )
    with_email = sum(1 for o in orders if _buyer_email(o))
    without_email = len(orders) - with_email
    with_shipment = sum(1 for o in orders if _shipment_recipient(o))
    with_pickup = sum(1 for o in orders if _pickup_recipient(o))
    multi_tender = sum(1 for o in orders if len(o.get("tenders") or []) > 1)
    li_discount = sum(1 for o in orders if _has_line_item_discount(o))
    order_discount = sum(1 for o in orders if _has_order_level_discount(o))
    li_counts = [len(o.get("line_items") or []) for o in orders]

    print("\n" + "=" * 70)
    print("=== SUMMARY ===")
    print("=" * 70)
    print(f"Total orders:                                {len(orders)}")
    print(f"By source.name:")
    for name, count in source_counter.most_common():
        print(f"  - {name!r}: {count}")
    print(f"buyer_email_address present:                 {with_email}")
    print(f"buyer_email_address missing:                 {without_email}")
    print(f"shipment recipient present:                  {with_shipment}")
    print(f"pickup recipient present:                    {with_pickup}")
    print(f"orders with multiple tenders (split pay):    {multi_tender}")
    print(f"orders with line-item-level discounts:       {li_discount}")
    print(f"orders with order-level discounts:           {order_discount}")
    if li_counts:
        print(
            f"line items per order:  min={min(li_counts)}  "
            f"max={max(li_counts)}  median={statistics.median(li_counts):g}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
