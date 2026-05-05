"""probes/probe_square_services.py — list APPOINTMENTS_SERVICE items.

Walks Square's full catalog and prints every item where
`item_data.product_type == "APPOINTMENTS_SERVICE"` with the
details needed to map them back to Linnworks SKUs (item id, name,
description; per-variation id, name, sku, pricing_type, price,
service_duration).

Read-only. No mutations against Square. No Supabase audit logging.

Run with:
    python -m probes.probe_square_services
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from lib import square


PAGE_LIMIT = 100
SERVICE_PRODUCT_TYPE = "APPOINTMENTS_SERVICE"


def _fetch_page(cursor: Optional[str]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "object_types": ["ITEM"],
        "limit": PAGE_LIMIT,
        "include_related_objects": True,
    }
    if cursor:
        body["cursor"] = cursor
    print(f"\n--- POST /catalog/search (cursor={cursor!r}) ---")
    print(f"    body: {json.dumps(body)}")
    result = square.call("catalog/search", method="POST", json_body=body)
    return result or {}


def _walk_catalog() -> list[dict[str, Any]]:
    """Returns the full list of raw ITEM objects whose product_type is
    APPOINTMENTS_SERVICE.
    """
    services: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    pages = 0
    items_walked = 0

    while True:
        pages += 1
        response = _fetch_page(cursor)
        objects = response.get("objects") or []
        page_services = 0
        for item in objects:
            items_walked += 1
            item_data = item.get("item_data") or {}
            if item_data.get("product_type") == SERVICE_PRODUCT_TYPE:
                services.append(item)
                page_services += 1
        print(
            f"    HTTP 200 — {len(objects)} item(s) returned, "
            f"+{page_services} services this page "
            f"(running totals: {len(services)} services / {items_walked} walked)"
        )
        cursor = response.get("cursor")
        if not cursor:
            print(f"    no cursor — last page reached after page {pages}")
            break

    return services


def _format_duration_ms(duration_ms: Optional[Any]) -> str:
    """Human-readable duration: '3600000 ms (60 min)'. Returns 'n/a'
    when missing or unparseable.
    """
    if duration_ms is None:
        return "n/a"
    try:
        ms = int(duration_ms)
    except (TypeError, ValueError):
        return f"{duration_ms!r} (not parseable)"
    minutes = ms / 60000
    return f"{ms} ms ({minutes:g} min)"


def _format_price(price_money: Optional[dict[str, Any]]) -> str:
    if not price_money:
        return "n/a"
    amount = price_money.get("amount")
    currency = price_money.get("currency") or ""
    if amount is None:
        return f"n/a (currency={currency!r})"
    return f"{amount} {currency}".strip()


def _print_service_block(idx: int, total: int, item: dict[str, Any]) -> None:
    item_data = item.get("item_data") or {}
    variations = item_data.get("variations") or []

    print(f"\n=== Service item {idx}/{total} ===")
    print(f"  id:           {item.get('id')}")
    print(f"  name:         {item_data.get('name')!r}")
    description = item_data.get("description")
    print(f"  description:  {description!r}" if description is not None else "  description:  (missing)")
    print(f"  variations:   {len(variations)}")

    for v_idx, var in enumerate(variations, start=1):
        var_data = var.get("item_variation_data") or {}
        print(f"    -- variation {v_idx}/{len(variations)} --")
        print(f"      id:               {var.get('id')}")
        print(f"      name:             {var_data.get('name')!r}")
        print(f"      sku:              {var_data.get('sku')!r}")
        print(f"      pricing_type:     {var_data.get('pricing_type')!r}")
        print(f"      price:            {_format_price(var_data.get('price_money'))}")
        print(f"      service_duration: {_format_duration_ms(var_data.get('service_duration'))}")


def main() -> int:
    print("--- probe_square_services (list all APPOINTMENTS_SERVICE items) ---")

    services = _walk_catalog()

    if not services:
        print("\n=== No APPOINTMENTS_SERVICE items found in the Square catalog. ===")
        return 0

    print("\n" + "=" * 70)
    print(f"=== {len(services)} APPOINTMENTS_SERVICE item(s) found ===")
    print("=" * 70)

    total_variations = 0
    for i, item in enumerate(services, start=1):
        _print_service_block(i, len(services), item)
        total_variations += len(((item.get("item_data") or {}).get("variations") or []))

    print("\n" + "=" * 70)
    print(
        f"=== SUMMARY: {len(services)} service item(s) covering "
        f"{total_variations} variation(s) ==="
    )
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
