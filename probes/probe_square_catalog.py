"""probes/probe_square_catalog.py — Phase 1 prep probe.

Phase 2 (stock-push) and Phase 1 (reconciliation report) both need
to know how SKUs join across the two systems. The hypothesis is
"the SKU string in Linnworks equals `item_variation_data.sku` in
Square". We need to confirm that against the live Northwest Guitars
catalog before writing any join code.

Read-only. No mutations, writes, or deletes against either platform.

## What it does

1. Sample 5 ITEM objects from Square via `POST /catalog/search`
   with `include_related_objects: true`. Print each item's id,
   name, variation count, and the FULL `item_variation_data` JSON
   for every variation (so we can eyeball the SKU shape, price
   structure, and any custom attributes).

2. Cross-check inventory: take the variation IDs from step 1 and
   call `POST /inventory/counts/batch-retrieve` against the pinned
   shop location `L74KSP08AJ2GH`. Empty response means the location
   ID is wrong; non-empty means it's correct.

3. Cross-reference Linnworks: take 2 SKU strings found in Square
   and call `Stock/GetStockItems` with `searchTypes: [0]` (search
   by SKU). Print whether each SKU was found.

4. Print `=== DISCOVERY: ===` lines summarising:
   - SKU join works (yes / no / partial)
   - Square location ID correct (yes / no)
   - Typical variation count per item
   - Whether any custom attributes appear

The output is meant to be eyeballed; nothing is written back to
DISCOVERIES.md automatically.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from lib import linnworks, square


# Pinned in Phase 0a. Inventory counts are per-location in Square.
SHOP_LOCATION_ID = "L74KSP08AJ2GH"

ITEMS_TO_SAMPLE = 5
SKUS_TO_CROSSCHECK = 2


# ---------- step 1: sample catalog items ----------


def _search_catalog_items(limit: int) -> dict[str, Any]:
    """POST /catalog/search — read-only catalog query."""
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


def _print_item_summary(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Pretty-print one item and return its variation dicts."""
    item_id = item.get("id")
    item_data = item.get("item_data", {}) if isinstance(item.get("item_data"), dict) else {}
    name = item_data.get("name")
    variations = item_data.get("variations", []) or []
    custom_attrs = item.get("custom_attribute_values") or {}

    print(f"\n  ━━━ ITEM ━━━")
    print(f"    id:               {item_id}")
    print(f"    item_data.name:   {name!r}")
    print(f"    variation count:  {len(variations)}")
    print(f"    custom_attribute_values: {json.dumps(custom_attrs, default=str)}")

    for i, var in enumerate(variations, start=1):
        var_data = var.get("item_variation_data", {}) if isinstance(var.get("item_variation_data"), dict) else {}
        print(f"\n    -- variation {i}/{len(variations)} --")
        print(f"      variation id:                       {var.get('id')}")
        print(f"      item_variation_data.name:           {var_data.get('name')!r}")
        print(f"      item_variation_data.sku:            {var_data.get('sku')!r}")
        print(f"      item_variation_data.price_money:    {json.dumps(var_data.get('price_money'), default=str)}")
        print(f"      FULL item_variation_data:           {json.dumps(var_data, default=str, indent=8)}")

    return variations


# ---------- step 2: inventory cross-check ----------


def _check_inventory(variation_ids: list[str]) -> dict[str, Any]:
    body = {
        "catalog_object_ids": variation_ids,
        "location_ids": [SHOP_LOCATION_ID],
    }
    print(f"\n--- step 2: POST /inventory/counts/batch-retrieve ---")
    print(f"    body: {json.dumps(body)}")
    result = square.call("inventory/counts/batch-retrieve", method="POST", json_body=body)
    counts = (result or {}).get("counts", []) or []
    print(f"    HTTP 200 — {len(counts)} count entr{'y' if len(counts) == 1 else 'ies'} returned")
    print(f"    full response: {json.dumps(result, default=str, indent=2)}")
    return result or {}


# ---------- step 3: linnworks cross-reference ----------


def _linnworks_find_sku(sku: str) -> tuple[bool, Optional[dict[str, Any]]]:
    """Call Stock/GetStockItems with the SKU as the keyword. Returns
    (found, first_match_dict). searchTypes=[0] is search-by-SKU per
    LINNWORKS_REFERENCE.md.
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
    print(f"\n  -- Linnworks lookup: keyword={sku!r} --")
    try:
        result = linnworks.call("Stock/GetStockItems", json_body=body)
    except Exception as e:
        print(f"     REQUEST FAILED: {type(e).__name__}: {e}")
        return (False, None)

    if not isinstance(result, list):
        print(f"     unexpected response shape ({type(result).__name__}): {json.dumps(result, default=str)[:300]}")
        return (False, None)

    print(f"     {len(result)} match(es)")
    if not result:
        return (False, None)
    first = result[0] if isinstance(result[0], dict) else {}
    print(
        f"     first match: SKU={first.get('ItemNumber')!r}  "
        f"Title={first.get('ItemTitle')!r}  "
        f"StockItemId={first.get('StockItemId')}"
    )
    # Linnworks keyword search may match prefix/contains rather than
    # exact — explicitly verify the returned ItemNumber equals our SKU.
    exact = any(isinstance(r, dict) and r.get("ItemNumber") == sku for r in result)
    print(f"     exact ItemNumber match for {sku!r}: {exact}")
    return (exact, first)


# ---------- main ----------


def main() -> int:
    print("--- probe_square_catalog (Phase 1 prep — SKU join discovery) ---")

    # Step 1
    search_response = _search_catalog_items(ITEMS_TO_SAMPLE)
    items = search_response.get("objects") or []
    if not items:
        print("=== DISCOVERY: /catalog/search returned 0 items — catalog may be empty ===")
        return 2

    print(f"\n=== sampled {len(items)} ITEM object(s) from Square catalog ===")
    all_variation_ids: list[str] = []
    all_skus: list[str] = []
    variation_counts: list[int] = []
    items_with_custom_attrs = 0
    for item in items:
        variations = _print_item_summary(item)
        variation_counts.append(len(variations))
        if item.get("custom_attribute_values"):
            items_with_custom_attrs += 1
        for var in variations:
            vid = var.get("id")
            if vid:
                all_variation_ids.append(vid)
            sku = (var.get("item_variation_data") or {}).get("sku")
            if sku:
                all_skus.append(sku)

    # Step 2
    if not all_variation_ids:
        print("=== DISCOVERY: no variations on any sampled item — cannot cross-check inventory ===")
        inventory_ok = False
    else:
        inv = _check_inventory(all_variation_ids)
        inventory_ok = bool((inv or {}).get("counts"))

    # Step 3
    print(f"\n--- step 3: cross-reference up to {SKUS_TO_CROSSCHECK} Square SKU(s) against Linnworks ---")
    sku_results: list[tuple[str, bool]] = []
    for sku in all_skus[:SKUS_TO_CROSSCHECK]:
        found, _ = _linnworks_find_sku(sku)
        sku_results.append((sku, found))
    if not sku_results:
        print("     no SKUs found in Square sample — nothing to cross-check")

    # ---------- step 4: discovery summary ----------
    print("\n" + "=" * 70)
    print("=== DISCOVERY SUMMARY ===")
    print("=" * 70)

    # SKU join verdict
    if not sku_results:
        sku_verdict = "no — Square sample had no SKUs to test"
    else:
        hits = sum(1 for _, ok in sku_results if ok)
        if hits == len(sku_results):
            sku_verdict = f"yes — {hits}/{len(sku_results)} Square SKUs matched ItemNumber in Linnworks exactly"
        elif hits == 0:
            sku_verdict = f"no — 0/{len(sku_results)} Square SKUs found in Linnworks (different SKU schemes)"
        else:
            sku_verdict = f"partial — {hits}/{len(sku_results)} Square SKUs matched (sample too small to be conclusive)"
    print(f"=== DISCOVERY: SKU join (Linnworks ItemNumber == Square item_variation_data.sku): {sku_verdict} ===")

    # Location verdict
    print(
        f"=== DISCOVERY: Square location ID {SHOP_LOCATION_ID!r} correct for inventory: "
        f"{'yes' if inventory_ok else 'no — inventory/counts/batch-retrieve returned empty'} ==="
    )

    # Variation count verdict
    if variation_counts:
        avg = sum(variation_counts) / len(variation_counts)
        multi = sum(1 for n in variation_counts if n > 1)
        if multi == 0:
            var_verdict = f"single variation per item (all {len(variation_counts)} sampled items have 1)"
        elif multi == len(variation_counts):
            var_verdict = f"multiple variations per item (all {len(variation_counts)} sampled items have >1; avg {avg:.1f})"
        else:
            var_verdict = f"mixed ({multi}/{len(variation_counts)} sampled items have >1 variation; avg {avg:.1f})"
    else:
        var_verdict = "no variations seen"
    print(f"=== DISCOVERY: variation pattern: {var_verdict} ===")

    # Custom attrs verdict
    print(
        f"=== DISCOVERY: custom attributes on items: "
        f"{'yes' if items_with_custom_attrs > 0 else 'no'} "
        f"({items_with_custom_attrs}/{len(items)} sampled items have custom_attribute_values) ==="
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
