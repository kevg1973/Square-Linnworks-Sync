"""probes/probe_square_catalog.py — Phase 1 prep probe (v3).

v2 sampled 50 items and reported all 50 as services, which can't
match reality — Kevin says there are only ~9 services in Square
Appointments. Either the classifier is wrong, the page-1 sample is
unlucky, or Square is returning some other shape we're not reading
right.

v3 stops trying to be clever about classification and just **shows
the data**:

1. Single-page dump of all 50 items returned by `/catalog/search`,
   one line each, exposing the raw fields the classifier was
   keying on (`available_for_booking`, `service_duration`,
   `team_member_ids` count, `stockable`) plus the name and SKU.
   This makes it obvious from the log whether v2's "all services"
   verdict was the classifier's fault or genuinely what Square
   returned.

2. Targeted lookup of a known retail SKU (`GB500-R-NK`, a Bass
   Tuner) via `exact_query → sku`. If exact_query returns nothing,
   fall back to `text_query → keywords`. Dump the FULL
   `item_variation_data` JSON for the match so we can see whether
   it has `available_for_booking: true` (which would mean the
   classifier signal is broken / Square sets that flag on retail).

3. Linnworks cross-reference for the same SKU.

4. Inventory call against the matched variation at location
   `L74KSP08AJ2GH`.

5. Discovery summary lines covering: how many of the 50 had
   `available_for_booking=true`, count of distinct names (services
   typically have many variations of the same name; retail
   doesn't), and the targeted-SKU verdicts.

Read-only. No mutations.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Any, Optional

from lib import linnworks, square


SHOP_LOCATION_ID = "L74KSP08AJ2GH"
ITEMS_TO_SAMPLE = 50
TARGET_SKU = "GB500-R-NK"


# ---------- step 1: bulk listing ----------


def _search_catalog_items(limit: int) -> dict[str, Any]:
    body = {
        "object_types": ["ITEM"],
        "limit": limit,
        "include_related_objects": True,
    }
    print(f"\n--- step 1: POST /catalog/search (limit={limit}) ---")
    print(f"    body: {json.dumps(body)}")
    result = square.call("catalog/search", method="POST", json_body=body)
    print(f"    HTTP 200 — top-level keys: {sorted((result or {}).keys())}")
    return result or {}


def _print_listing(items: list[dict[str, Any]]) -> None:
    """One line per item with the raw fields the classifier was
    keying on. If an item has multiple variations the first is shown
    and a `[N variations]` marker is appended.
    """
    print(f"\n--- per-item one-liners ({len(items)} items) ---")
    for i, item in enumerate(items, start=1):
        item_data = item.get("item_data") or {}
        name = item_data.get("name")
        variations = item_data.get("variations") or []
        var = variations[0] if variations else {}
        var_data = var.get("item_variation_data") or {}

        sku = var_data.get("sku")
        afb = var_data.get("available_for_booking")
        sd = var_data.get("service_duration")
        tmi = var_data.get("team_member_ids") or []
        stockable = var_data.get("stockable")

        var_marker = f"  [{len(variations)} variations]" if len(variations) > 1 else ""
        print(
            f"[{i:>2}] name={name!r} | sku={sku!r} | "
            f"available_for_booking={afb} | "
            f"service_duration={sd!r} | "
            f"team_member_ids={len(tmi)} | "
            f"stockable={stockable}{var_marker}"
        )


# ---------- step 2: targeted SKU lookup ----------


def _print_full_variation(item: dict[str, Any], var: dict[str, Any], header: str) -> None:
    item_data = item.get("item_data") or {}
    var_data = var.get("item_variation_data") or {}
    print(f"\n  ━━━ {header} ━━━")
    print(f"    item id:                 {item.get('id')}")
    print(f"    item_data.name:          {item_data.get('name')!r}")
    print(f"    variation id:            {var.get('id')}")
    print(f"    item_variation_data.sku: {var_data.get('sku')!r}")
    print(f"    FULL item_variation_data:")
    print(json.dumps(var_data, default=str, indent=6))


def _square_lookup_sku(target: str) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Try exact_query first, fall back to text_query. Returns (item,
    variation) for the first match whose SKU == target, or (None, None).
    """
    print(f"\n--- step 2: targeted lookup of SKU {target!r} ---")

    exact_body = {
        "object_types": ["ITEM"],
        "query": {
            "exact_query": {
                "attribute_name": "sku",
                "attribute_value": target,
            }
        },
        "limit": 5,
    }
    print(f"    POST /catalog/search exact_query: {json.dumps(exact_body)}")
    try:
        result = square.call("catalog/search", method="POST", json_body=exact_body)
    except square.SquareError as e:
        print(f"    REQUEST FAILED: {e}")
        result = None
    objects = (result or {}).get("objects") or []
    print(f"    exact_query returned {len(objects)} object(s)")

    if not objects:
        text_body = {
            "object_types": ["ITEM"],
            "query": {
                "text_query": {
                    "keywords": [target],
                }
            },
            "limit": 5,
        }
        print(f"    falling back to text_query: {json.dumps(text_body)}")
        try:
            result = square.call("catalog/search", method="POST", json_body=text_body)
        except square.SquareError as e:
            print(f"    REQUEST FAILED: {e}")
            result = None
        objects = (result or {}).get("objects") or []
        print(f"    text_query returned {len(objects)} object(s)")

    if not objects:
        return (None, None)

    # Prefer the variation whose SKU matches exactly.
    for item in objects:
        variations = (item.get("item_data") or {}).get("variations") or []
        for var in variations:
            sku = (var.get("item_variation_data") or {}).get("sku")
            if sku == target:
                _print_full_variation(item, var, header=f"MATCHED VARIATION for SKU {target!r}")
                return (item, var)

    # No exact SKU match — show the first object so we can see what
    # the keyword search latched onto.
    item = objects[0]
    variations = (item.get("item_data") or {}).get("variations") or []
    var = variations[0] if variations else {}
    print(f"    NOTE: no exact SKU match in returned objects — showing first")
    _print_full_variation(item, var, header=f"FIRST RESULT (no exact SKU match for {target!r})")
    return (item, var if var else None)


# ---------- step 3: Linnworks cross-reference ----------


def _linnworks_find_sku(sku: str) -> tuple[bool, Optional[dict[str, Any]]]:
    """Stock/GetStockItems with searchTypes:[0] (search by SKU).

    Response shape: {PageNumber, EntriesPerPage, TotalEntries,
    TotalPages, Data: [<stock item>, ...]}.
    """
    body = {
        "keyword": sku,
        "loadCompositeParents": False,
        "loadVariationParents": False,
        "entriesPerPage": 5,
        "pageNumber": 1,
        "dataRequirements": [],
        "searchTypes": [0],
    }
    print(f"\n--- step 3: Linnworks Stock/GetStockItems lookup for {sku!r} ---")
    try:
        result = linnworks.call("Stock/GetStockItems", json_body=body)
    except Exception as e:
        print(f"    REQUEST FAILED: {type(e).__name__}: {e}")
        return (False, None)

    if not isinstance(result, dict):
        print(f"    unexpected response shape ({type(result).__name__}): {json.dumps(result, default=str)[:300]}")
        return (False, None)

    total = result.get("TotalEntries", 0)
    data = result.get("Data") or []
    print(f"    TotalEntries={total}, returned {len(data)} record(s) in Data")

    if total <= 0 or not data:
        return (False, None)

    first = data[0] if isinstance(data[0], dict) else {}
    print(
        f"    first match: ItemNumber={first.get('ItemNumber')!r}  "
        f"ItemTitle={first.get('ItemTitle')!r}  "
        f"Quantity={first.get('Quantity')}  "
        f"IsNotTrackable={first.get('IsNotTrackable')}"
    )
    exact = any(isinstance(r, dict) and r.get("ItemNumber") == sku for r in data)
    print(f"    exact ItemNumber match for {sku!r}: {exact}")
    return (exact, first)


# ---------- step 4: inventory ----------


def _check_inventory(variation_id: str) -> Optional[int]:
    """Returns the quantity at SHOP_LOCATION_ID for one variation, or
    None if the call returned no count entries.
    """
    body = {
        "catalog_object_ids": [variation_id],
        "location_ids": [SHOP_LOCATION_ID],
    }
    print(f"\n--- step 4: POST /inventory/counts/batch-retrieve for variation {variation_id} ---")
    print(f"    body: {json.dumps(body)}")
    try:
        result = square.call("inventory/counts/batch-retrieve", method="POST", json_body=body)
    except square.SquareError as e:
        print(f"    REQUEST FAILED: {e}")
        return None

    counts = (result or {}).get("counts") or []
    print(f"    HTTP 200 — {len(counts)} count entr{'y' if len(counts) == 1 else 'ies'} returned")
    print(f"    full response: {json.dumps(result, default=str, indent=2)}")
    if not counts:
        return None
    qty = counts[0].get("quantity")
    try:
        return int(qty)
    except (TypeError, ValueError):
        return None


# ---------- main ----------


def main() -> int:
    print("--- probe_square_catalog (v3 — flat listing + targeted SKU) ---")

    # ---------- step 1: dump all 50 ----------
    search_response = _search_catalog_items(ITEMS_TO_SAMPLE)
    items = search_response.get("objects") or []
    if not items:
        print("=== DISCOVERY: /catalog/search returned 0 items — catalog may be empty ===")
        return 2
    _print_listing(items)

    # Counts for the discovery summary
    n_bookable = 0
    for item in items:
        variations = (item.get("item_data") or {}).get("variations") or []
        if any(
            (v.get("item_variation_data") or {}).get("available_for_booking") is True
            for v in variations
        ):
            n_bookable += 1

    names = [(item.get("item_data") or {}).get("name") for item in items]
    name_counter = Counter(names)
    distinct_names = len(name_counter)
    top_names = name_counter.most_common(3)

    # ---------- step 2: targeted SKU lookup ----------
    target_item, target_var = _square_lookup_sku(TARGET_SKU)

    # ---------- step 3: Linnworks lookup ----------
    found_in_lw, lw_record = _linnworks_find_sku(TARGET_SKU)

    # ---------- step 4: inventory for the matched variation ----------
    inv_qty: Optional[int] = None
    if target_var and target_var.get("id"):
        inv_qty = _check_inventory(target_var["id"])
    else:
        print("\n--- step 4: skipped — no Square variation id to query ---")

    # ---------- step 5: discovery summary ----------
    print("\n" + "=" * 70)
    print("=== DISCOVERY SUMMARY ===")
    print("=" * 70)

    print(
        f"=== DISCOVERY: Number of items in catalog/search page 1 with "
        f"available_for_booking=true: {n_bookable} out of {len(items)} ==="
    )

    top_str = ", ".join(f"{name!r} x {count}" for name, count in top_names)
    print(
        f"=== DISCOVERY: Distinct item names in page 1 sample: {distinct_names} "
        f"(top: {top_str}) ==="
    )

    print(
        f"=== DISCOVERY: Targeted SKU {TARGET_SKU!r} found in Square: "
        f"{'yes' if target_var else 'no'} ==="
    )

    if target_var:
        var_data = target_var.get("item_variation_data") or {}
        afb = var_data.get("available_for_booking")
        sd = var_data.get("service_duration")
        tmi = var_data.get("team_member_ids") or []
        print(
            f"=== DISCOVERY: {TARGET_SKU!r} Square fields: "
            f"available_for_booking={afb}, service_duration={sd!r}, "
            f"team_member_ids={len(tmi)} ==="
        )
    else:
        print(f"=== DISCOVERY: {TARGET_SKU!r} Square fields: n/a (not found) ===")

    print(
        f"=== DISCOVERY: {TARGET_SKU!r} found in Linnworks: "
        f"{'yes' if found_in_lw else 'no'} ==="
    )

    if lw_record is not None:
        print(
            f"=== DISCOVERY: {TARGET_SKU!r} Linnworks IsNotTrackable: "
            f"{lw_record.get('IsNotTrackable')} ==="
        )
    else:
        print(f"=== DISCOVERY: {TARGET_SKU!r} Linnworks IsNotTrackable: n/a ===")

    if inv_qty is not None:
        print(
            f"=== DISCOVERY: {TARGET_SKU!r} inventory at location "
            f"{SHOP_LOCATION_ID!r}: {inv_qty} ==="
        )
    else:
        print(
            f"=== DISCOVERY: {TARGET_SKU!r} inventory at location "
            f"{SHOP_LOCATION_ID!r}: n/a ==="
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
