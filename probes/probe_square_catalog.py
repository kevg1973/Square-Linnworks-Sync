"""probes/probe_square_catalog.py — Phase 1 prep probe (v2).

Phase 1 (reconciliation) and Phase 2 (stock-push) both need to know
how SKUs join across the two systems and what the catalog actually
looks like. v1 sampled 5 items but they were all Square Appointments
**services** (Standard Guitar Setup, Headstock Repair, etc), not
retail products — so the SKU join verdict was inconclusive. v2
broadens the sample to 50 and categorises before printing so we see
the catalog mix.

Read-only. No mutations, writes, or deletes against either platform.

## What it does

1. Fetch up to 50 ITEM objects from Square via `POST /catalog/search`
   with `include_related_objects: true`.

2. Classify each item:
   - **service**: any variation has `available_for_booking: true`,
     `service_duration` set, or `team_member_ids` populated.
   - **multi-variation**: not a service AND has >1 variation.
   - **retail**: everything else (one variation, no service markers).

3. Print a summary line and then up to 3 representative examples
   per category — the FULL `item_variation_data` JSON for each
   variation so we can eyeball the SKU/price/attribute shape.

4. Cross-check inventory: take the variation IDs from the first 5
   retail items and call `POST /inventory/counts/batch-retrieve`
   against the pinned shop location `L74KSP08AJ2GH`. Empty response
   means the location ID is wrong.

5. Cross-reference Linnworks: the same 5 retail SKUs go through
   `Stock/GetStockItems` with `searchTypes: [0]` (search by SKU).
   The response shape is `{"PageNumber": ..., "Data": [...],
   "TotalEntries": ..., ...}` — v1 mis-parsed this as a bare list,
   v2 reads `Data[0]` when `TotalEntries > 0`.

6. Print `=== DISCOVERY: ===` lines summarising catalog
   composition, SKU join verdict, location ID correctness,
   unmatched SKU list, multi-variation pattern, and the
   `IsNotTrackable` values from matched Linnworks records (so we
   can tell whether Linnworks marks services / non-stock items as
   untracked).

The output is meant to be eyeballed; nothing is written back to
DISCOVERIES.md automatically.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from lib import linnworks, square


SHOP_LOCATION_ID = "L74KSP08AJ2GH"

ITEMS_TO_SAMPLE = 50
EXAMPLES_PER_CATEGORY = 3
RETAIL_SKUS_TO_CROSSCHECK = 5


# ---------- step 1: sample catalog items ----------


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


# ---------- step 2: classification ----------


def _is_service_variation(var: dict[str, Any]) -> bool:
    var_data = var.get("item_variation_data") or {}
    if var_data.get("available_for_booking") is True:
        return True
    if var_data.get("service_duration") is not None:
        return True
    if var_data.get("team_member_ids"):
        return True
    return False


def _classify(item: dict[str, Any]) -> str:
    variations = (item.get("item_data") or {}).get("variations") or []
    if any(_is_service_variation(v) for v in variations):
        return "service"
    if len(variations) > 1:
        return "multi-variation"
    return "retail"


# ---------- step 3: per-item printing ----------


def _print_item_full(item: dict[str, Any], header: str) -> None:
    item_id = item.get("id")
    item_data = item.get("item_data") or {}
    name = item_data.get("name")
    variations = item_data.get("variations") or []
    custom_attrs = item.get("custom_attribute_values") or {}

    print(f"\n  ━━━ {header} ━━━")
    print(f"    id:               {item_id}")
    print(f"    item_data.name:   {name!r}")
    print(f"    variation count:  {len(variations)}")
    print(f"    custom_attribute_values: {json.dumps(custom_attrs, default=str)}")

    for i, var in enumerate(variations, start=1):
        var_data = var.get("item_variation_data") or {}
        print(f"\n    -- variation {i}/{len(variations)} --")
        print(f"      variation id:                       {var.get('id')}")
        print(f"      item_variation_data.name:           {var_data.get('name')!r}")
        print(f"      item_variation_data.sku:            {var_data.get('sku')!r}")
        print(f"      item_variation_data.price_money:    {json.dumps(var_data.get('price_money'), default=str)}")
        print(f"      FULL item_variation_data:           {json.dumps(var_data, default=str, indent=8)}")


# ---------- step 4: inventory cross-check ----------


def _check_inventory(variation_ids: list[str]) -> dict[str, Any]:
    body = {
        "catalog_object_ids": variation_ids,
        "location_ids": [SHOP_LOCATION_ID],
    }
    print(f"\n--- step 4: POST /inventory/counts/batch-retrieve (retail variations only) ---")
    print(f"    body: {json.dumps(body)}")
    result = square.call("inventory/counts/batch-retrieve", method="POST", json_body=body)
    counts = (result or {}).get("counts", []) or []
    print(f"    HTTP 200 — {len(counts)} count entr{'y' if len(counts) == 1 else 'ies'} returned")
    print(f"    full response: {json.dumps(result, default=str, indent=2)}")
    return result or {}


# ---------- step 5: linnworks cross-reference ----------


def _linnworks_find_sku(sku: str) -> tuple[bool, Optional[dict[str, Any]]]:
    """Call Stock/GetStockItems with the SKU as the keyword. Returns
    (exact_match_found, first_data_dict).

    Response shape (per a prior run):
      {"PageNumber": int, "EntriesPerPage": int, "TotalEntries": int,
       "TotalPages": int, "Data": [<stock item>, ...]}
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

    if not isinstance(result, dict):
        print(f"     unexpected response shape ({type(result).__name__}): {json.dumps(result, default=str)[:300]}")
        return (False, None)

    total = result.get("TotalEntries", 0)
    data = result.get("Data") or []
    print(f"     TotalEntries={total}, returned {len(data)} record(s) in Data")

    if total <= 0 or not data:
        return (False, None)

    first = data[0] if isinstance(data[0], dict) else {}
    print(
        f"     first match: ItemNumber={first.get('ItemNumber')!r}  "
        f"ItemTitle={first.get('ItemTitle')!r}  "
        f"Quantity={first.get('Quantity')}  "
        f"IsNotTrackable={first.get('IsNotTrackable')}"
    )

    # Linnworks keyword search may match prefix/contains, not exact.
    exact = any(isinstance(r, dict) and r.get("ItemNumber") == sku for r in data)
    print(f"     exact ItemNumber match for {sku!r}: {exact}")
    return (exact, first)


# ---------- main ----------


def main() -> int:
    print("--- probe_square_catalog (v2 — broader sample, retail focus) ---")

    search_response = _search_catalog_items(ITEMS_TO_SAMPLE)
    items = search_response.get("objects") or []
    if not items:
        print("=== DISCOVERY: /catalog/search returned 0 items — catalog may be empty ===")
        return 2

    # ---------- step 2 — classify ----------
    by_category: dict[str, list[dict[str, Any]]] = {
        "service": [],
        "multi-variation": [],
        "retail": [],
    }
    for item in items:
        by_category[_classify(item)].append(item)

    n_total = len(items)
    n_service = len(by_category["service"])
    n_multi = len(by_category["multi-variation"])
    n_retail = len(by_category["retail"])

    print("\n" + "=" * 70)
    print(
        f"=== Sampled {n_total} items: "
        f"{n_service} service, {n_multi} multi-variation, {n_retail} retail ==="
    )
    print("=" * 70)

    # ---------- step 3 — print up to 3 examples per category ----------
    for category in ("service", "multi-variation", "retail"):
        bucket = by_category[category]
        if not bucket:
            print(f"\n=== category '{category}': none in sample ===")
            continue
        print(
            f"\n=== category '{category}': showing "
            f"{min(EXAMPLES_PER_CATEGORY, len(bucket))} of {len(bucket)} ==="
        )
        for i, item in enumerate(bucket[:EXAMPLES_PER_CATEGORY], start=1):
            _print_item_full(item, header=f"{category.upper()} #{i}")

    # ---------- pick the 5 retail items for cross-checking ----------
    retail_picks = by_category["retail"][:RETAIL_SKUS_TO_CROSSCHECK]
    retail_variation_ids: list[str] = []
    retail_skus: list[str] = []
    for item in retail_picks:
        variations = (item.get("item_data") or {}).get("variations") or []
        if not variations:
            continue
        var = variations[0]
        var_data = var.get("item_variation_data") or {}
        vid = var.get("id")
        sku = var_data.get("sku")
        if vid:
            retail_variation_ids.append(vid)
        if sku:
            retail_skus.append(sku)

    # ---------- step 4 — inventory cross-check (retail only) ----------
    if not retail_variation_ids:
        print("\n=== no retail variations available for inventory cross-check ===")
        inventory_ok = False
        inventory_count = 0
    else:
        inv = _check_inventory(retail_variation_ids)
        counts = (inv or {}).get("counts") or []
        inventory_count = len(counts)
        inventory_ok = inventory_count > 0

    # ---------- step 5 — linnworks cross-reference (retail SKUs) ----------
    print(
        f"\n--- step 5: cross-reference up to {RETAIL_SKUS_TO_CROSSCHECK} "
        f"retail Square SKU(s) against Linnworks ---"
    )
    sku_results: list[tuple[str, bool, Optional[dict[str, Any]]]] = []
    for sku in retail_skus:
        found, record = _linnworks_find_sku(sku)
        sku_results.append((sku, found, record))
    if not sku_results:
        print("     no retail SKUs found in Square sample — nothing to cross-check")

    matched = [(s, r) for s, ok, r in sku_results if ok and r is not None]
    unmatched_skus = [s for s, ok, _ in sku_results if not ok]
    is_not_trackable_values = [r.get("IsNotTrackable") for _, r in matched]

    # ---------- step 6 — discovery summary ----------
    print("\n" + "=" * 70)
    print("=== DISCOVERY SUMMARY ===")
    print("=" * 70)

    print(
        f"=== DISCOVERY: Catalog composition: "
        f"{n_service} service, {n_multi} multi-variation, {n_retail} retail "
        f"(out of {n_total} sampled) ==="
    )

    if not sku_results:
        sku_verdict = "n/a — no retail SKUs in sample to test"
    else:
        hits = sum(1 for _, ok, _ in sku_results if ok)
        verdict_word = (
            "yes" if hits == len(sku_results)
            else "no" if hits == 0
            else "partial"
        )
        sku_verdict = f"{verdict_word} — {hits}/{len(sku_results)} retail SKUs found in Linnworks"
    print(
        f"=== DISCOVERY: SKU join (retail Square SKU == Linnworks ItemNumber): "
        f"{sku_verdict} ==="
    )

    print(
        f"=== DISCOVERY: Square location {SHOP_LOCATION_ID!r} returns inventory "
        f"for retail items: "
        f"{'yes — ' + str(inventory_count) + ' counts returned' if inventory_ok else 'no — empty response'} ==="
    )

    if unmatched_skus:
        print(
            f"=== DISCOVERY: For unmatched SKUs, the SKU strings were: "
            f"{json.dumps(unmatched_skus)} ==="
        )
    else:
        print(
            "=== DISCOVERY: For unmatched SKUs, the SKU strings were: "
            "[] (no unmatched SKUs in sample) ==="
        )

    if n_multi == 0:
        multi_verdict = "none in sample"
    else:
        per_item = []
        for it in by_category["multi-variation"][:EXAMPLES_PER_CATEGORY]:
            variations = (it.get("item_data") or {}).get("variations") or []
            sku_list = [
                (v.get("item_variation_data") or {}).get("sku") for v in variations
            ]
            per_item.append(
                f"{(it.get('item_data') or {}).get('name')!r} → "
                f"{len(variations)} variations, SKUs={json.dumps(sku_list)}"
            )
        multi_verdict = f"{n_multi} multi-variation items in sample. Examples: " + " | ".join(per_item)
    print(f"=== DISCOVERY: Multi-variation pattern: {multi_verdict} ===")

    print(
        f"=== DISCOVERY: Linnworks IsNotTrackable values for matched SKUs: "
        f"{json.dumps(is_not_trackable_values)} ==="
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
