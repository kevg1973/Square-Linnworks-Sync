"""probes/probe_square_duplicates.py — Phase 1 prep probe (v2).

Walks the entire Square catalog and groups by SKU to count
duplicates. We need this number to scope the cleanup design — if
duplicate SKUs are common, the reconciliation report (Phase 1) and
the stock-push (Phase 2) both need explicit handling for "which of
the N items with this SKU should we update?".

v1 walked 2000 items across 20 pages and found zero duplicates,
but Kevin has visually confirmed at least one duplicate
(`GB500-R-NK` appears twice in the Square inventory dashboard).
Either the catalog is larger than 2000 items and the cap cut off
the duplicate-rich zone, or the walk silently misses items somehow.

v2:
- Raises the cap to 100 pages (~10,000 items).
- Tracks a running total per page so we can see if pages are
  uniformly full (true pagination) or returning fewer.
- Logs every variation that's skipped for null/empty SKU with its
  item id and name (v1 reported only 1 skip but said "General
  Appointment" had `sku: None` — inconsistent, so we want to see
  exactly what's being dropped).
- Adds a targeted-search verification step at the end: looks up
  `GB500-R-NK` via `/catalog/search exact_query` (with text_query
  fallback) and reconciles against what the paginated walk found.
  If the walk shows 0 but the targeted search shows 2+, prints
  `=== ANOMALY: ... ===` so the contradiction is impossible to miss.

Read-only. No mutations.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from typing import Any, Optional

from lib import square


PAGE_LIMIT = 100
PAGE_CAP = 100  # safety stop at ~10,000 items
DUPLICATES_TO_PRINT = 20
SKIPPED_TO_PRINT = 10
TARGET_SKU = "GB500-R-NK"


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


def _targeted_lookup(target: str) -> list[dict[str, Any]]:
    """Returns the list of objects from /catalog/search that contain a
    variation whose SKU equals `target` exactly. Tries exact_query
    first, falls back to text_query.
    """
    print(f"\n--- targeted lookup: SKU {target!r} ---")

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

    # Filter to objects that actually contain a variation with the
    # exact SKU (text_query may return loose matches).
    matches: list[dict[str, Any]] = []
    for item in objects:
        variations = (item.get("item_data") or {}).get("variations") or []
        for var in variations:
            sku = (var.get("item_variation_data") or {}).get("sku")
            if sku == target:
                matches.append(item)
                break

    print(f"    {len(matches)} object(s) contain a variation with SKU == {target!r}")
    for m in matches:
        item_data = m.get("item_data") or {}
        # Find the matching variation for the print
        sku_var_id = None
        for v in item_data.get("variations") or []:
            if (v.get("item_variation_data") or {}).get("sku") == target:
                sku_var_id = v.get("id")
                break
        print(
            f"      - id={m.get('id')}, "
            f"name={item_data.get('name')!r}, "
            f"variation_id={sku_var_id}"
        )
    return matches


def main() -> int:
    print("--- probe_square_duplicates v2 (cap=100 pages, +targeted verification) ---")

    # sku -> list of {item_id, variation_id, item_name}
    by_sku: dict[str, list[dict[str, str]]] = defaultdict(list)
    skipped: list[dict[str, Any]] = []  # all variations dropped for null/empty SKU

    cursor: Optional[str] = None
    pages = 0
    items_walked = 0
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
        items_walked_before = items_walked

        for item in objects:
            items_walked += 1
            item_id = item.get("id")
            item_name = (item.get("item_data") or {}).get("name") or ""
            variations = (item.get("item_data") or {}).get("variations") or []
            for var in variations:
                var_data = var.get("item_variation_data") or {}
                sku = var_data.get("sku")
                if not sku:
                    skipped.append({
                        "item_id": item_id,
                        "variation_id": var.get("id"),
                        "item_name": item_name,
                    })
                    continue
                by_sku[sku].append({
                    "item_id": item_id,
                    "variation_id": var.get("id"),
                    "item_name": item_name,
                })

        page_count = items_walked - items_walked_before
        print(
            f"    HTTP 200 — {len(objects)} ITEM object(s) returned, "
            f"+{page_count} walked, running total {items_walked}"
        )

        cursor = response.get("cursor")
        if not cursor:
            print(f"    no cursor — last page reached after page {pages}")
            break

    # ---------- group analysis ----------
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
    print(f"Variations skipped (null/empty SKU): {len(skipped)}")
    print(f"Total distinct SKUs (non-null):   {n_distinct_skus}")
    print(f"SKUs with 1 item:                 {n_unique} (unique)")
    print(f"SKUs with 2 items:                {n_pair} (duplicate pairs)")
    print(f"SKUs with 3+ items:               {n_multi} (multi-duplicates)")

    if duplicates_sorted:
        n_show = min(DUPLICATES_TO_PRINT, len(duplicates_sorted))
        print(f"\n=== First {n_show} duplicate SKUs (sorted by match count desc) ===")
        for sku, matches in duplicates_sorted[:DUPLICATES_TO_PRINT]:
            print(f"SKU {sku!r} — {len(matches)} matches:")
            for m in matches:
                print(f"  - id={m['item_id']}, name={m['item_name']!r}")
    else:
        print("\n=== No duplicate SKUs found in the paginated walk. ===")

    # ---------- targeted verification ----------
    targeted_matches = _targeted_lookup(TARGET_SKU)
    walk_matches = sku_to_distinct_items.get(TARGET_SKU, [])

    # ---------- discovery summary ----------
    print("\n" + "=" * 70)
    print("=== DISCOVERY SUMMARY ===")
    print("=" * 70)
    pages_completed = pages - (1 if cap_hit else 0)
    print(
        f"=== DISCOVERY: Catalog walk completed: {items_walked} items across "
        f"{pages_completed} pages (cap hit: {'yes' if cap_hit else 'no'}) ==="
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
        print("=== DISCOVERY: Worst offender: n/a (no duplicates in walk) ===")

    skipped_preview = [
        (s["item_id"], s["item_name"]) for s in skipped[:SKIPPED_TO_PRINT]
    ]
    print(
        f"=== DISCOVERY: First {min(SKIPPED_TO_PRINT, len(skipped))} of "
        f"{len(skipped)} skipped variations: {json.dumps(skipped_preview)} ==="
    )

    print(
        f"=== DISCOVERY: {TARGET_SKU!r} found via paginated walk: "
        f"{'yes' if walk_matches else 'no'} (count: {len(walk_matches)}) ==="
    )
    print(
        f"=== DISCOVERY: {TARGET_SKU!r} found via targeted search: "
        f"{'yes' if targeted_matches else 'no'} (count: {len(targeted_matches)}) ==="
    )

    if len(walk_matches) == 0 and len(targeted_matches) >= 2:
        print(
            f"=== ANOMALY: paginated walk missed items found by targeted search "
            f"(walk={len(walk_matches)}, targeted={len(targeted_matches)}) — "
            f"the /catalog/search pagination is not returning the full catalog ==="
        )
    elif len(walk_matches) != len(targeted_matches):
        print(
            f"=== ANOMALY: walk count ({len(walk_matches)}) != targeted count "
            f"({len(targeted_matches)}) — investigate ==="
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
