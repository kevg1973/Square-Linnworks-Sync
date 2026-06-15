"""tools/pull_square_orders_to_linnworks.py — Phase 3.

Pulls COMPLETED Square POS orders updated since the last
watermark and creates matching orders in Linnworks as a
**three-step recipe** per DISCOVERIES.md §4:

  1. POST Orders/CreateOrders         (JSON)              — create
  2. POST Orders/ChangeOrderTag       (form-encoded)      — unpark
  3. POST Orders/ChangeStatus status=1 (form-encoded)     — mark paid

Steps 2+3 are critical: orders created via CreateOrders land
parked, and ChangeStatus silently no-ops on parked orders. Without
the unpark, the order would sit unprocessable in Linnworks.

Idempotency at the Square order id level via
sq_square_orders_processed plus Linnworks' own (Source, SubSource,
ReferenceNumber) natural dedup as a backstop. The bookkeeping row
is only inserted after **all three** Linnworks calls succeed —
partial-success orders (e.g. create OK, unpark fails) are not
recorded as processed and are retried on the next run. Linnworks'
natural dedup means re-creating returns the same pkOrderID, so
retries are cheap.

## Safety posture

- Default observe-mode (--write to actually call Linnworks
  CreateOrders). Observe runs print the planned payload for the
  first few orders, never write to Linnworks, never insert into
  sq_square_orders_processed, and never advance the watermark.
- Per-order failures don't crash the run — they're logged to
  sq_errors via lib.db.log_error and the next order is attempted.
- The watermark only advances after a successful write run with
  ≥1 order processed. New value = max(updated_at) over the
  successfully-created orders. Failed orders stay in-window for
  the next run to retry.
- Audit row written to sq_orders_pull_log per run.

## Watermark logic

- Read from sq_watermarks where key='square_orders_pulled'.
- If absent: default to now − 7 days (initial backfill window).
- Pull window: updated_at >= (watermark − 60 seconds). The 60s
  back-step handles Square's eventual consistency on
  updated_at without re-doing too much work.

## SKU resolution

- Per Square line_item: catalog_object_id is the Square ITEM_VARIATION
  id. Looked up in sq_sku_map.square_variation_id → sku.
- Misses (e.g. Appointments services not in our retail catalog,
  ad-hoc POS items) fall back to using line_item.name as the
  SKU stand-in. Linnworks treats unknown SKUs as ad-hoc lines
  (no stock movement) — matching the legacy POS app's behaviour
  for services.

## Linnworks order shape

Per the locked-in CreateOrders recipe in DISCOVERIES.md §3:

  - Source = "SQUAREPOS"
  - SubSource = "# <square-order-id>"
  - ReferenceNumber = ExternalReferenceNumber = <square-order-id>
  - LocationId = 00000000-0000-0000-0000-000000000000 (Default)
  - Currency = GBP
  - DeliveryAddress = BillingAddress (same shape; placeholder
    fallback for missing fields — POS sales typically have no
    shipping recipient).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from lib import db, linnworks, square


# ---------- constants ----------

WATERMARK_KEY = "square_orders_pulled"
WATERMARK_BACKSTEP_SECONDS = 60
DEFAULT_LOOKBACK_DAYS = 7

SHOP_LOCATION_ID = "L74KSP08AJ2GH"
LINNWORKS_DEFAULT_LOCATION = "00000000-0000-0000-0000-000000000000"
JOB_NAME = "pull-square-orders"

ORDER_PAGE_LIMIT = 100
SKU_MAP_PAGE_SIZE = 1000
PROCESSED_LOOKUP_CHUNK = 200
PLAN_PREVIEW = 5

# Placeholder for fields the Square order doesn't supply. POS sales
# rarely include a shipment recipient, so we fall back to the shop's
# own address — keeps Linnworks happy without leaking customer data
# we never had.
PLACEHOLDER_ADDRESS: dict[str, str] = {
    "FullName":     "Square Customer",
    "Company":      "",
    "EmailAddress": "orders@northwestguitars.co.uk",
    "PhoneNumber":  "0700000000",
    "Address1":     "Unit A",
    "Address2":     "Hoyle Street",
    "Address3":     "",
    "Town":         "Warrington",
    "Region":       "Cheshire",
    "PostCode":     "WA5 0LW",
    "Country":      "United Kingdom",
    "CountryCode":  "GB",
}

# Minimal ISO-code → country-name map. Extend as needed; falls back
# to the code itself for anything not listed.
COUNTRY_NAME: dict[str, str] = {
    "GB": "United Kingdom",
    "US": "United States",
    "IE": "Ireland",
    "FR": "France",
    "DE": "Germany",
    "ES": "Spain",
    "IT": "Italy",
    "NL": "Netherlands",
    "BE": "Belgium",
    "DK": "Denmark",
    "SE": "Sweden",
    "NO": "Norway",
    "FI": "Finland",
    "PL": "Poland",
    "AU": "Australia",
    "NZ": "New Zealand",
    "CA": "Canada",
    "JP": "Japan",
}


# ---------- time helpers ----------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ---------- watermark ----------


def _read_watermark() -> datetime:
    raw = db.get_watermark(WATERMARK_KEY)
    parsed = _parse_iso(raw) if raw else None
    if parsed:
        return parsed
    return _now_utc() - timedelta(days=DEFAULT_LOOKBACK_DAYS)


def _save_watermark(ts: datetime) -> None:
    db.set_watermark(WATERMARK_KEY, ts.isoformat())


# ---------- sku map ----------


def _load_sku_map() -> dict[str, dict[str, Optional[str]]]:
    """Returns variation_id → {"sku": <sku>, "linnworks_item_id": <uuid>}
    for every row in sq_sku_map. Pre-loaded once per run.

    The Linnworks StockItemId UUID is included so each Square order
    line can carry a strong-link reference back to the Linnworks
    inventory record — without it, Linnworks shows the line as
    "Unlinked item" and the tenant's process/dispatch flow blocks.
    SKU text-match alone is insufficient.
    """
    sku_map: dict[str, dict[str, Optional[str]]] = {}
    offset = 0
    while True:
        resp = (
            db.client()
            .table("sq_sku_map")
            .select("sku, square_variation_id, linnworks_item_id")
            .range(offset, offset + SKU_MAP_PAGE_SIZE - 1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            break
        for r in rows:
            vid = r.get("square_variation_id")
            sku = r.get("sku")
            if vid and sku:
                sku_map[vid] = {
                    "sku": sku,
                    "linnworks_item_id": r.get("linnworks_item_id"),
                }
        if len(rows) < SKU_MAP_PAGE_SIZE:
            break
        offset += SKU_MAP_PAGE_SIZE
    return sku_map


# ---------- Square pull ----------


def _search_orders_page(start_at: str, cursor: Optional[str]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "location_ids": [SHOP_LOCATION_ID],
        "query": {
            "filter": {
                "date_time_filter": {
                    "updated_at": {
                        "start_at": start_at,
                    },
                },
                "state_filter": {
                    "states": ["COMPLETED"],
                },
            },
            "sort": {
                "sort_field": "UPDATED_AT",
                "sort_order": "ASC",
            },
        },
        "limit": ORDER_PAGE_LIMIT,
    }
    if cursor:
        body["cursor"] = cursor
    return square.call("orders/search", method="POST", json_body=body) or {}


def _walk_orders(start_at: str) -> list[dict[str, Any]]:
    print(f"\n--- pulling Square orders updated_at >= {start_at} ---")
    orders: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    pages = 0
    while True:
        pages += 1
        resp = _search_orders_page(start_at, cursor)
        page = resp.get("orders") or []
        orders.extend(page)
        print(f"    page {pages}: +{len(page)} order(s) (running total {len(orders)})")
        cursor = resp.get("cursor")
        if not cursor:
            break
    return orders


# ---------- idempotency filter ----------


def _filter_already_processed(
    orders: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns (new_orders, already_processed_orders)."""
    if not orders:
        return ([], [])
    ids = [o.get("id") for o in orders if o.get("id")]
    already: set[str] = set()
    for i in range(0, len(ids), PROCESSED_LOOKUP_CHUNK):
        chunk = ids[i:i + PROCESSED_LOOKUP_CHUNK]
        try:
            resp = (
                db.client()
                .table("sq_square_orders_processed")
                .select("square_order_id")
                .in_("square_order_id", chunk)
                .execute()
            )
            for r in resp.data or []:
                if r.get("square_order_id"):
                    already.add(r["square_order_id"])
        except Exception as e:
            sys.stderr.write(
                f"[idempotency lookup FAILED chunk {i // PROCESSED_LOOKUP_CHUNK + 1}] {e}\n"
            )
    new = [o for o in orders if o.get("id") not in already]
    skipped = [o for o in orders if o.get("id") in already]
    return (new, skipped)


# ---------- Linnworks payload construction ----------


def _extract_recipient(order: dict[str, Any]) -> Optional[dict[str, Any]]:
    fulfillments = order.get("fulfillments") or []
    if not fulfillments:
        return None
    f0 = fulfillments[0] or {}
    ship = (f0.get("shipment_details") or {}).get("recipient")
    if ship:
        return ship
    pickup = (f0.get("pickup_details") or {}).get("recipient")
    if pickup:
        return pickup
    return None


def _country_full(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return COUNTRY_NAME.get(code.upper(), code)


def _build_address(order: dict[str, Any]) -> dict[str, str]:
    rec = _extract_recipient(order) or {}
    addr = rec.get("address") or {}
    return {
        "FullName":     rec.get("display_name")  or PLACEHOLDER_ADDRESS["FullName"],
        "Company":      "",
        "EmailAddress": (
            rec.get("email_address")
            or order.get("buyer_email_address")
            or PLACEHOLDER_ADDRESS["EmailAddress"]
        ),
        "PhoneNumber":  rec.get("phone_number")  or PLACEHOLDER_ADDRESS["PhoneNumber"],
        "Address1":     addr.get("address_line_1") or PLACEHOLDER_ADDRESS["Address1"],
        "Address2":     addr.get("address_line_2") or PLACEHOLDER_ADDRESS["Address2"],
        "Address3":     "",
        "Town":         addr.get("locality")       or PLACEHOLDER_ADDRESS["Town"],
        "Region":       addr.get("administrative_district_level_1") or PLACEHOLDER_ADDRESS["Region"],
        "PostCode":     addr.get("postal_code")    or PLACEHOLDER_ADDRESS["PostCode"],
        "Country":      _country_full(addr.get("country")) or PLACEHOLDER_ADDRESS["Country"],
        "CountryCode":  (addr.get("country") or PLACEHOLDER_ADDRESS["CountryCode"]).upper(),
    }


def _build_order_items(
    order: dict[str, Any],
    sku_map: dict[str, dict[str, Optional[str]]],
    *,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for li in order.get("line_items") or []:
        cat_id = li.get("catalog_object_id") or ""
        entry = sku_map.get(cat_id) if cat_id else None

        sku: Optional[str] = None
        stock_item_id: Optional[str] = None
        if entry:
            sku = entry.get("sku")
            stock_item_id = entry.get("linnworks_item_id")

        if not sku:
            # Service or ad-hoc line — fall back to the line name so
            # Linnworks still records something sensible. No StockItemId
            # attached: the line will land "unlinked" and won't move
            # stock, which is correct for non-stock-tracked services.
            title = (li.get("name") or "").strip()
            if verbose:
                print(
                    f"    [SKU lookup miss] catalog_object_id={cat_id!r} "
                    f"falling back to title={title!r} (will be unlinked)"
                )
            sku = title or "UNKNOWN"
        else:
            if verbose:
                print(
                    f"    [SKU lookup hit] catalog_object_id={cat_id!r} → "
                    f"sku={sku!r} stockitem={stock_item_id!r}"
                )
        sku = sku.strip()

        try:
            qty = int(li.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        if qty < 1:
            qty = 1

        total_pence = 0
        try:
            total_pence = int((li.get("total_money") or {}).get("amount") or 0)
        except (TypeError, ValueError):
            pass
        price_per_unit = round((total_pence / 100) / qty, 4)

        # Hit = the catalog_object_id resolved to a sq_sku_map row
        # (i.e. it's a real product we know about). Miss = service /
        # ad-hoc line falling back to title-as-SKU.
        is_hit = entry is not None

        order_item: dict[str, Any] = {
            "SKU":          sku,
            "ChannelSKU":   sku,
            "ItemNumber":   sku,
            "ItemTitle":    (li.get("name") or "").strip() or "Item",
            "Qty":          qty,
            "PricePerUnit": price_per_unit,
            "Discount":     0,
            "LineDiscount": 0,
            # VAT: shop is UK VAT-registered; product prices in
            # Square are 20% VAT-inclusive, services are exempt.
            # TaxCostInclusive=true tells Linnworks to back-calculate
            # the VAT out of the gross PricePerUnit instead of
            # adding it on top. UseChannelTax=true honours the rate
            # we send rather than overriding from product/category
            # defaults.
            "TaxRate":         20 if is_hit else 0,
            "TaxCostInclusive": True,
            "UseChannelTax":   True,
            # isService=false marks the line as a real stock-tracked
            # product so Linnworks attempts to link it; isService=true
            # tells Linnworks not to expect a stock link (services,
            # ad-hoc lines).
            "isService":    not is_hit,
        }
        # Strong-link to the Linnworks inventory record. Only set
        # when the lookup hit AND we have a UUID — Linnworks rejects
        # empty/null StockItemId values, and unlinked services
        # shouldn't carry a stale or fabricated UUID.
        if stock_item_id:
            order_item["StockItemId"] = stock_item_id

        items.append(order_item)
    return items


def _merge_duplicate_skus(order_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Linnworks Orders/CreateOrders rejects orders containing duplicate SKUs
    across line items (it returns e.g. "LineId: SPR22 is duplicated"). Square
    allows the same product to be rung up as multiple separate lines. Merge
    same-SKU lines into one with combined qty, deriving PricePerUnit from the
    summed line totals so per-line price variations (e.g. a partial discount
    on one line) are preserved correctly.

    The merge key is the SKU itself, so it also collapses service / unmapped
    lines that fell back to title-as-SKU — exactly the right behaviour, since
    Linnworks' duplicate check is on the SKU value regardless of stock-link
    status.
    """
    merged: dict[str, dict[str, Any]] = {}
    for item in order_items:
        sku = item["SKU"]
        if sku in merged:
            existing = merged[sku]
            combined_qty = existing["Qty"] + item["Qty"]
            # Sum each line's own total (qty × price) before dividing, so
            # differing per-unit prices across the same-SKU lines average
            # out by value rather than by a naive per-unit mean.
            combined_total = (
                existing["Qty"] * existing["PricePerUnit"]
                + item["Qty"] * item["PricePerUnit"]
            )
            existing["Qty"] = combined_qty
            existing["PricePerUnit"] = round(combined_total / combined_qty, 4)
            print(
                f"    [merge] combined duplicate SKU '{sku}' → "
                f"qty {combined_qty} @ £{existing['PricePerUnit']}"
            )
        else:
            merged[sku] = item
    return list(merged.values())


def _build_linnworks_payload(
    order: dict[str, Any],
    sku_map: dict[str, dict[str, Optional[str]]],
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    sq_id = order["id"]
    received = _parse_iso(order.get("created_at")) or _now_utc()
    dispatch_by = received + timedelta(days=2)
    address = _build_address(order)

    order_items = _merge_duplicate_skus(
        _build_order_items(order, sku_map, verbose=verbose)
    )

    return {
        "Source":                  "SQUAREPOS",
        "SubSource":               f"# {sq_id}",
        "ReferenceNumber":         sq_id,
        "ExternalReferenceNumber": sq_id,
        "ReceivedDate":            received.isoformat(),
        "DispatchBy":              dispatch_by.isoformat(),
        "LocationId":              LINNWORKS_DEFAULT_LOCATION,
        "Currency":                "GBP",
        # Per Linnworks docs: without this top-level flag, Linnworks
        # won't auto-link line items to stock records even when the
        # SKU matches exactly. Required alongside StockItemId on the
        # OrderItems for the line to land linked.
        "AutomaticallyLinkBySKU":  True,
        # Honour the per-line TaxRate / TaxCostInclusive flags
        # rather than letting Linnworks override from
        # product/category defaults. Set at both order and
        # line-item level per Linnworks API docs.
        "UseChannelTax":           True,
        "OrderItems":              order_items,
        "DeliveryAddress":         address,
        "BillingAddress":          dict(address),
    }


# ---------- Linnworks call ----------


def _create_linnworks_order(payload: dict[str, Any]) -> str:
    """POST Orders/CreateOrders for one order. Returns the pkOrderID
    string. Per DISCOVERIES.md §3, the response is a bare JSON array
    of pkOrderID strings.
    """
    body = {"orders": [payload]}
    resp = linnworks.call("Orders/CreateOrders", json_body=body)
    if isinstance(resp, list) and resp and isinstance(resp[0], str):
        return resp[0]
    raise RuntimeError(f"unexpected CreateOrders response shape: {resp!r}")


def _unpark_linnworks_order(pk_order_id: str) -> None:
    """POST Orders/ChangeOrderTag to unpark. Per DISCOVERIES.md §4:
    application/x-www-form-urlencoded with body
    `orderIds=["<uuid>"]` (the value is a JSON-encoded array as a
    string, then URL-encoded by the HTTP client). No other fields —
    the endpoint name itself implies the unpark action.
    """
    form = {"orderIds": json.dumps([pk_order_id])}
    linnworks.call("Orders/ChangeOrderTag", form_body=form)


def _mark_paid_linnworks_order(pk_order_id: str) -> None:
    """POST Orders/ChangeStatus status=1 (Paid) per DISCOVERIES.md §4.
    Form-encoded. **MUST** run after the unpark — parked orders
    silently no-op ChangeStatus (returns 200, doesn't change Status).
    """
    form = {"orderIds": json.dumps([pk_order_id]), "status": "1"}
    linnworks.call("Orders/ChangeStatus", form_body=form)


# ---------- bookkeeping helpers ----------


def _record_processed(square_id: str, lw_pk: str, order: dict[str, Any]) -> None:
    total_pence = 0
    try:
        total_pence = int((order.get("total_money") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        pass
    rec = _extract_recipient(order) or {}
    customer_name = rec.get("display_name") or "Square Customer"
    db.client().table("sq_square_orders_processed").insert({
        "square_order_id":    square_id,
        "linnworks_order_id": lw_pk,
        "total":              f"{total_pence / 100:.2f}",
        "customer_name":      customer_name,
        "status":             "created",
    }).execute()


def _log_per_order_error(square_id: str, message: str, context: dict[str, Any]) -> None:
    ctx = {"square_order_id": square_id}
    ctx.update(context)
    db.log_error(JOB_NAME, message, ctx)


def _write_audit_row(
    *,
    mode: str,
    watermark_before: Optional[datetime],
    watermark_after: Optional[datetime],
    orders_fetched: int,
    orders_processed: int,
    orders_skipped: int,
    orders_skipped_empty: int,
    orders_failed: int,
    orders_created: int,
    orders_unparked: int,
    orders_marked_paid: int,
    error_messages: list[str],
) -> None:
    summary = ""
    if error_messages:
        summary = " | ".join(m[:100] for m in error_messages[:3])
        if len(error_messages) > 3:
            summary += f" | (+{len(error_messages) - 3} more)"
    payload = {
        "run_at":               _now_iso(),
        "mode":                 mode,
        "watermark_before":     watermark_before.isoformat() if watermark_before else None,
        "watermark_after":      watermark_after.isoformat() if watermark_after else None,
        "orders_fetched":       orders_fetched,
        "orders_processed":     orders_processed,
        "orders_skipped":       orders_skipped,
        "orders_skipped_empty": orders_skipped_empty,
        "orders_failed":        orders_failed,
        "orders_created":       orders_created,
        "orders_unparked":      orders_unparked,
        "orders_marked_paid":   orders_marked_paid,
        "error_summary":        summary[:1000] if summary else None,
    }
    try:
        db.client().table("sq_orders_pull_log").insert(payload).execute()
        print("    audit row written to sq_orders_pull_log")
    except Exception as e:
        sys.stderr.write(f"[sq_orders_pull_log insert FAILED] {e}\n")


# ---------- preview ----------


def _print_payload_preview(order: dict[str, Any], payload: dict[str, Any]) -> None:
    sq_id = order.get("id")
    sq_total = ((order.get("total_money") or {}).get("amount") or 0) / 100
    print(f"\n  ━━━ {sq_id} (total £{sq_total:.2f}, {len(payload['OrderItems'])} line(s)) ━━━")
    print("  Step 1: POST /api/Orders/CreateOrders  (JSON)")
    print(json.dumps({"orders": [payload]}, indent=2, default=str))
    print('  Step 2: POST /api/Orders/ChangeOrderTag  (form-encoded)')
    print('    body: orderIds=["<pkOrderID from step 1>"]')
    print('  Step 3: POST /api/Orders/ChangeStatus  (form-encoded)')
    print('    body: orderIds=["<pkOrderID from step 1>"]&status=1')


# ---------- main ----------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pull_square_orders_to_linnworks",
        description=(
            "Pulls COMPLETED Square POS orders updated since the "
            "watermark and creates matching orders in Linnworks. "
            "Default mode is OBSERVE (dry run, prints planned "
            "payloads). Pass --write to actually call CreateOrders "
            "and advance the watermark."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually POST to Linnworks. Without this, observe-only.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Cap on orders to process this run. Useful for staged rollouts.",
    )
    args = parser.parse_args(argv)

    mode = "write" if args.write else "observe"
    print(
        f"\n{'!' * 70}\n"
        f"!!  pull_square_orders_to_linnworks — mode={mode.upper()}\n"
        f"!!  {'will WRITE Linnworks orders + advance watermark' if args.write else 'DRY RUN — no Linnworks writes, watermark frozen'}\n"
        f"{'!' * 70}\n"
    )

    # ---------- preload ----------
    print("--- loading sq_sku_map ---")
    sku_map = _load_sku_map()
    print(f"    {len(sku_map)} variation_id → sku mapping(s) loaded")
    if sku_map:
        # Sanity check: keys should look like Square variation IDs
        # (uppercase alphanum, ~26 chars). If they look wrong here,
        # the bug is in _load_sku_map.
        sample_key = next(iter(sku_map))
        print(f"    sample mapping: {sample_key!r} → {sku_map[sample_key]!r}")

    watermark_before = _read_watermark()
    window_start_dt = watermark_before - timedelta(seconds=WATERMARK_BACKSTEP_SECONDS)
    window_start = window_start_dt.isoformat()
    print(f"\nwatermark (sq_watermarks[{WATERMARK_KEY!r}]): {watermark_before.isoformat()}")
    print(f"pull window: updated_at >= {window_start} (back-stepped {WATERMARK_BACKSTEP_SECONDS}s)")

    # ---------- pull ----------
    orders = _walk_orders(window_start)
    fetched = len(orders)
    if args.limit is not None:
        if args.limit < 0:
            print(f"ERROR: --limit must be non-negative, got {args.limit}")
            return 2
        if args.limit < len(orders):
            print(f"\n--- --limit {args.limit} applied: trimming from {len(orders)} → {args.limit} ---")
            orders = orders[:args.limit]

    # ---------- idempotency ----------
    new_orders, skipped_orders = _filter_already_processed(orders)

    # Filter out orders Square returned with zero line items —
    # creating empty orders in Linnworks isn't useful. Don't insert
    # into sq_square_orders_processed (so if Square later attaches
    # line items, we'll re-evaluate); don't log to sq_errors (it's
    # not a failure, just empty). Surface the count in SUMMARY +
    # audit row.
    empty_orders = [o for o in new_orders if not (o.get("line_items") or [])]
    new_orders = [o for o in new_orders if (o.get("line_items") or [])]
    skipped_empty_count = len(empty_orders)

    print(
        f"\n=== fetched={fetched}  to_process={len(new_orders)}  "
        f"already_processed={len(skipped_orders)}  "
        f"skipped_empty={skipped_empty_count} ==="
    )
    if empty_orders:
        for o in empty_orders[:PLAN_PREVIEW]:
            print(f"  empty order skipped: {o.get('id')!r}")

    # ---------- PLAN ----------
    print("\n" + "=" * 70)
    print("=== PLAN ===")
    print("=" * 70)
    print(f"Will attempt to create {len(new_orders)} Linnworks order(s).")
    if new_orders:
        n_show = min(PLAN_PREVIEW, len(new_orders))
        print(f"\n--- previewing first {n_show} payload(s) ---")
        for o in new_orders[:PLAN_PREVIEW]:
            try:
                payload = _build_linnworks_payload(o, sku_map, verbose=True)
                _print_payload_preview(o, payload)
            except Exception as e:
                print(f"  ! could not build payload for {o.get('id')!r}: {e}")

    # ---------- observe → exit ----------
    if not args.write:
        print(f"\n=== DRY RUN — no Linnworks writes performed, watermark unchanged. ===")
        _write_audit_row(
            mode=mode,
            watermark_before=watermark_before,
            watermark_after=None,
            orders_fetched=fetched,
            orders_processed=0,
            orders_skipped=len(skipped_orders),
            orders_skipped_empty=skipped_empty_count,
            orders_failed=0,
            orders_created=0,
            orders_unparked=0,
            orders_marked_paid=0,
            error_messages=[],
        )
        print(
            f"\n=== PULL COMPLETE: fetched={fetched} processed=0 "
            f"skipped={len(skipped_orders)} skipped_empty={skipped_empty_count} failed=0 "
            f"created=0 unparked=0 marked_paid=0 "
            f"watermark_before={watermark_before.isoformat()} watermark_after=(unchanged) "
            f"(observe mode) ==="
        )
        return 0

    # ---------- write ----------
    print(
        f"\n=== WRITE MODE — about to run the 3-step recipe (create → unpark → mark paid) for "
        f"{len(new_orders)} order(s) ==="
    )

    successful: list[tuple[dict[str, Any], str]] = []
    failed: list[tuple[dict[str, Any], str]] = []
    error_messages: list[str] = []
    created_count = 0
    unparked_count = 0
    marked_paid_count = 0

    for order in new_orders:
        sq_id = order.get("id") or "(no id)"

        # Build payload
        try:
            payload = _build_linnworks_payload(order, sku_map)
        except Exception as e:
            msg = f"payload build failed: {e}"
            _log_per_order_error(sq_id, msg, {"step": "build"})
            error_messages.append(f"{sq_id}: {msg}")
            failed.append((order, msg))
            print(f"  ✗ {sq_id} → {msg[:200]}")
            continue

        # Step 1 — create
        try:
            pk = _create_linnworks_order(payload)
        except Exception as e:
            msg = f"CreateOrders failed: {e}"
            _log_per_order_error(
                sq_id,
                msg,
                {
                    "step": "create",
                    "order_item_skus": [
                        it.get("SKU") for it in payload.get("OrderItems", [])
                    ],
                },
            )
            error_messages.append(f"{sq_id}: {msg}")
            failed.append((order, msg))
            print(f"  ✗ {sq_id} create failed → {str(e)[:800]}")
            # Dump the exact payload Linnworks rejected as ONE atomic
            # block. Logged this way, a "was the JSON malformed?"
            # question is answerable straight from the run log — the
            # serialized object here is exactly what went on the wire.
            print("    ----- payload Linnworks rejected (atomic dump) -----")
            print(json.dumps({"orders": [payload]}, indent=2, default=str))
            print("    ----------------------------------------------------")
            continue
        created_count += 1
        print(f"  ✓ {sq_id} created → pkOrderID {pk}")

        # Step 2 — unpark. Failure here means the order exists in
        # Linnworks but is still parked. We don't insert into the
        # processed table, so the next run re-attempts; CreateOrders
        # dedup returns the same pk cheaply.
        try:
            _unpark_linnworks_order(pk)
        except Exception as e:
            msg = f"create succeeded, unpark failed for pkOrderID={pk}: {e}"
            _log_per_order_error(
                sq_id, msg, {"step": "unpark", "linnworks_pk": pk}
            )
            error_messages.append(f"{sq_id}: {msg}")
            failed.append((order, msg))
            print(f"    ✗ unpark failed → {str(e)[:200]}")
            continue
        unparked_count += 1
        print(f"    ✓ unparked")

        # Step 3 — mark paid. Same recovery story: skip bookkeeping,
        # next run retries the chain.
        try:
            _mark_paid_linnworks_order(pk)
        except Exception as e:
            msg = f"create+unpark succeeded, mark-paid failed for pkOrderID={pk}: {e}"
            _log_per_order_error(
                sq_id, msg, {"step": "mark_paid", "linnworks_pk": pk}
            )
            error_messages.append(f"{sq_id}: {msg}")
            failed.append((order, msg))
            print(f"    ✗ mark-paid failed → {str(e)[:200]}")
            continue
        marked_paid_count += 1
        print(f"    ✓ marked paid")

        # Bookkeeping insert. Linnworks side is fully done; if this
        # fails we still count the order as successful so the
        # watermark advances. The natural (Source, SubSource,
        # ReferenceNumber) dedup protects us if the row never lands
        # and the order is re-pulled later (within the 60s back-step
        # or before the watermark moves past it).
        try:
            _record_processed(sq_id, pk, order)
        except Exception as e:
            sys.stderr.write(
                f"[sq_square_orders_processed insert FAILED for {sq_id} → {pk}] {e}\n"
            )
            error_messages.append(f"{sq_id}: bookkeeping insert failed: {e}")

        successful.append((order, pk))

    # ---------- watermark ----------
    watermark_after: Optional[datetime] = None
    if successful:
        candidates = [
            _parse_iso(o.get("updated_at"))
            for o, _ in successful
        ]
        candidates = [c for c in candidates if c is not None]
        if candidates:
            watermark_after = max(candidates)
            try:
                _save_watermark(watermark_after)
                print(f"\nwatermark advanced: {watermark_before.isoformat()} → {watermark_after.isoformat()}")
            except Exception as e:
                sys.stderr.write(f"[watermark save FAILED] {e}\n")
                error_messages.append(f"watermark save failed: {e}")
                watermark_after = None

    # ---------- audit + summary ----------
    _write_audit_row(
        mode=mode,
        watermark_before=watermark_before,
        watermark_after=watermark_after,
        orders_fetched=fetched,
        orders_processed=len(successful),
        orders_skipped=len(skipped_orders),
        orders_skipped_empty=skipped_empty_count,
        orders_failed=len(failed),
        orders_created=created_count,
        orders_unparked=unparked_count,
        orders_marked_paid=marked_paid_count,
        error_messages=error_messages,
    )

    print("\n" + "=" * 70)
    print(
        f"=== PULL COMPLETE: fetched={fetched} processed={len(successful)} "
        f"skipped={len(skipped_orders)} skipped_empty={skipped_empty_count} failed={len(failed)} "
        f"created={created_count} unparked={unparked_count} marked_paid={marked_paid_count} "
        f"watermark_before={watermark_before.isoformat()} "
        f"watermark_after={watermark_after.isoformat() if watermark_after else '(unchanged)'} ==="
    )
    print("=" * 70)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
