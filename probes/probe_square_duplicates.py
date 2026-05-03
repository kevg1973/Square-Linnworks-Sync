"""probes/probe_square_duplicates.py — Phase 1 prep probe.

Walks the entire Square catalog and groups by SKU to count
duplicates. We need this number to scope the cleanup design — if
duplicate SKUs are common, the reconciliation report (Phase 1) and
the stock-push (Phase 2) both need explicit handling for "which of
the N items with this SKU should we update?".

Read-only. No mutations against either platform. Pure paginated
read of `/catalog/search` plus an in-memory group-by.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from typing import Any, Optional

from lib import square


PAGE_LIMIT = 100
PAGE_CAP = 20  # safety stop at ~2000 items
DUPLICATES_TO_PRINT = 20


def _fetch_page(cursor: Optional[str]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "object_types": ["ITEM"],
        "limit": PAGE_LIMIT,
        "include_related_objects": True,
    }
    if cursor:
        body["cursor"] = cursor
    result = square.call("catalog/search", method="POST", json_body=body)
    return result or {}


def main() -> int:
    print("--- probe_square_duplicates (catalog-wide SKU duplicate scan) ---")

    # sku -> list of {item_id, variation_id, item_name}
    by_sku: dict[str, list[dict[str, str]]] = defaultdict(list)

    cursor: Optional[str] = None
    pages = 0
    items_walked = 0
    skipped_null_sku = 0
    cap_hit = False

    while True:
        pages += 1
        if pages > PAGE_CAP:
            cap_hit = True
            print(f"\n!!! page cap ({PAGE_CAP}) hit — catalog may be larger than expected !!!")
            break

        print(f"\n--- fetching page {pages} (cursor={cursor!r}) ---")
        response = _fetch_page(cursor)
        objects = response.get("objects") or []
        print(f"    HTTP 200 — {len(objects)} ITEM object(s) returned")

        for item in objects:
            items_walked += 1
            item_id = item.get("id")
            item_name = (item.get("item_data") or {}).get("name") or ""
            variations = (item.get("item_data") or {}).get("variations") or []
            for var in variations:
                var_data = var.get("item_variation_data") or {}
                sku = var_data.get("sku")
                if not sku:
                    skipped_null_sku += 1
                    continue
                by_sku[sku].append({
                    "item_id": item_id,
                    "variation_id": var.get("id"),
                    "item_name": item_name,
                })

        cursor = response.get("cursor")
        if not cursor:
            print(f"    no cursor — last page reached after page {pages}")
            break

    # ---------- group analysis ----------
    # Treat duplicates as same SKU on >1 distinct item_id (a single
    # item with two same-SKU variations is unusual but shouldn't
    # count as a catalog duplicate).
    sku_to_distinct_items: dict[str, list[dict[str, str]]] = {}
    for sku, matches in by_sku.items():
        seen: set[str] = set()
        deduped = []
        for m in matches:
            if m["item_id"] in seen:
                continue
            seen.add(m["item_id"])
            deduped.append(m)
        sku_to_distinct_items[sku] = deduped

    n_distinct_skus = len(sku_to_distinct_items)
    n_unique = sum(1 for ms in sku_to_distinct_items.values() if len(ms) == 1)
    n_pair = sum(1 for ms in sku_to_distinct_items.values() if len(ms) == 2)
    n_multi = sum(1 for ms in sku_to_distinct_items.values() if len(ms) >= 3)
    n_duplicate_skus = n_pair + n_multi
    extra_items = sum(len(ms) - 1 for ms in sku_to_distinct_items.values() if len(ms) >= 2)

    duplicates_sorted = sorted(
        ((sku, ms) for sku, ms in sku_to_distinct_items.items() if len(ms) >= 2),
        key=lambda x: -len(x[1]),
    )

    # ---------- duplicate summary ----------
    print("\n" + "=" * 70)
    print("=== DUPLICATE SUMMARY ===")
    print("=" * 70)
    print(f"Total catalog items walked:       {items_walked}")
    print(f"Variations skipped (null/empty SKU): {skipped_null_sku}")
    print(f"Total distinct SKUs (non-null):   {n_distinct_skus}")
    print(f"SKUs with 1 item:                 {n_unique} (unique)")
    print(f"SKUs with 2 items:                {n_pair} (duplicate pairs)")
    print(f"SKUs with 3+ items:               {n_multi} (multi-duplicates)")

    # ---------- top N duplicates ----------
    if duplicates_sorted:
        n_show = min(DUPLICATES_TO_PRINT, len(duplicates_sorted))
        print(f"\n=== First {n_show} duplicate SKUs (sorted by match count desc) ===")
        for sku, matches in duplicates_sorted[:DUPLICATES_TO_PRINT]:
            print(f"SKU {sku!r} — {len(matches)} matches:")
            for m in matches:
                print(f"  - id={m['item_id']}, name={m['item_name']!r}")
    else:
        print("\n=== No duplicate SKUs found in the catalog. ===")

    # ---------- discovery summary ----------
    print("\n" + "=" * 70)
    print("=== DISCOVERY SUMMARY ===")
    print("=" * 70)
    print(
        f"=== DISCOVERY: Catalog walk completed: {items_walked} items across "
        f"{pages - (1 if cap_hit else 0)} pages (cap hit: {'yes' if cap_hit else 'no'}) ==="
    )
    print(f"=== DISCOVERY: Distinct SKUs: {n_distinct_skus} ===")
    print(
        f"=== DISCOVERY: Duplicate SKUs (2+ matches): {n_duplicate_skus} total, "
        f"affecting {extra_items} extra items ==="
    )
    if duplicates_sorted:
        worst_sku, worst_matches = duplicates_sorted[0]
        print(
            f"=== DISCOVERY: Worst offender: SKU {worst_sku!r} has "
            f"{len(worst_matches)} matches ==="
        )
    else:
        print("=== DISCOVERY: Worst offender: n/a (no duplicates) ===")

    return 0


if __name__ == "__main__":
    sys.exit(main())
