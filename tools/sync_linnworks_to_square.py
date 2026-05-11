"""tools/sync_linnworks_to_square.py — Phase 1, step 2.

Pulls every stock item from Linnworks, walks the Square catalog,
and reconciles: creates retail items in Square that don't exist
yet, updates ones whose name or price has drifted, and pushes
current stock counts at the Northwest Guitars location.

## Safety posture

- **Default is observe-mode** (a dry run that prints the plan and
  exits). You must pass `--write` to actually call Square's write
  endpoints.
- `--limit N` processes only the first N Linnworks items, useful
  for staged rollouts and testing.
- Square Appointments services are never touched — we only walk /
  match against `product_type == "REGULAR"` items in Square.
- Items with null/empty Linnworks SKU are skipped (we can't sync
  what we can't key on).
- Audit row written to `sq_lw_sync_log` for every run (observe or
  write). Audit insert failures fall back to stderr — they don't
  crash the sync.

## Classification

For each Linnworks item:

- **CREATE** — SKU not present in Square's REGULAR catalog.
- **UPDATE** — SKU present AND (name or price differs).
- **STOCK_ONLY** — SKU present, name+price already match, but
  current Square stock != desired Linnworks stock.
- **NO_OP** — name+price match AND stock matches.

Stock is pushed for CREATE, UPDATE, and STOCK_ONLY. NO_OP items
involve no API writes at all.

## Wire-format details that bit on this build

- `Stock/GetStockItemsFull` may return a bare list OR a dict with
  `Data: [...]`. The pull handles both.
- Square `batch-upsert` requires `version` on UPDATEs (optimistic
  concurrency). Both the item version AND each variation's version
  must come from the live catalog walk.
- For CREATEs the response's `id_mappings` array maps our temp IDs
  (`#var_<sku>`) to the real `catalog_object_id`s — that's how we
  learn the new variation IDs to push stock against.
- Inventory writes use `PHYSICAL_COUNT` changes against the
  pinned shop location `L74KSP08AJ2GH`.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from lib import db, linnworks, square


SHOP_LOCATION_ID = "L74KSP08AJ2GH"
DEFAULT_LINNWORKS_LOCATION_ID = "00000000-0000-0000-0000-000000000000"

LW_ENTRIES_PER_PAGE = 200
LW_PAGE_SAFETY_CAP = 500  # 100k items — should never be reached

SQUARE_PAGE_LIMIT = 100
INVENTORY_FETCH_CHUNK = 500
UPSERT_BATCH_SIZE = 100
STOCK_BATCH_SIZE = 100
SKU_MAP_BATCH_SIZE = 500
SLEEP_BETWEEN_BATCHES = 0.2

PREVIEW_PER_CATEGORY = 20

PRODUCT_TYPE_REGULAR = "REGULAR"

# Linnworks items we never want to sync to Square. Filtered at
# ingestion time (inside _pull_linnworks_items) so they never enter
# the working set. Keep these lists narrow and amend deliberately.
EXCLUDED_CATEGORY_SUBSTRINGS = frozenset({
    "reverse auction",
    "competition winners",
    "competition prizes",
    "b stock",
})
EXCLUDED_SKUS = frozenset({
    "Custom Order",
    "571-2828362",
    "571-2828365",
    "GTR-001",
})


# ---------- Linnworks pull ----------


def _extract_default_qty(stock_levels: list[dict[str, Any]]) -> int:
    """Sum StockLevel for the Default location (StockLocationId zero
    UUID). Handles both the nested {Location:{StockLocationId}} and
    the flat {StockLocationId} shapes.
    """
    total = 0
    for entry in stock_levels or []:
        loc_id = None
        if isinstance(entry.get("Location"), dict):
            loc_id = entry["Location"].get("StockLocationId")
        if loc_id is None:
            loc_id = entry.get("StockLocationId")
        if loc_id == DEFAULT_LINNWORKS_LOCATION_ID:
            try:
                total += int(entry.get("StockLevel") or 0)
            except (TypeError, ValueError):
                pass
    return total


def _fetch_lw_page(page_number: int) -> list[dict[str, Any]]:
    body = {
        "keyword": "",
        "loadCompositeParents": False,
        "loadVariationParents": False,
        "entriesPerPage": LW_ENTRIES_PER_PAGE,
        "pageNumber": page_number,
        "dataRequirements": [0, 1, 2],
        "searchTypes": [],
    }
    try:
        result = linnworks.call("Stock/GetStockItemsFull", json_body=body)
    except requests.HTTPError as e:
        # Linnworks signals end-of-pagination with HTTP 400 when you
        # walk one page off the end. Treat that as an empty page so
        # the caller stops cleanly. A 400 on page 1 means the request
        # body itself is wrong — re-raise so it surfaces loudly.
        if e.response is not None and e.response.status_code == 400 and page_number > 1:
            return []
        raise
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        data = result.get("Data")
        if isinstance(data, list):
            return data
    return []


def _pull_linnworks_items() -> list[dict[str, Any]]:
    """Returns list of {sku, name, price_pence, barcode, qty}.

    End-of-catalog detection. Linnworks paginates Stock/GetStockItemsFull
    by index — pageNumber walks 1..N. Two end signals exist on this
    tenant and we handle both:

      1. Partial page (fewer than entriesPerPage items returned).
         The common case — stop cleanly after processing it.
      2. HTTP 400 on a page that walks off the end. Hit when the
         catalog total is an exact multiple of entriesPerPage, so
         the previous page was full and we ask for one more. Handled
         inside _fetch_lw_page (returns []) — that path falls through
         to the empty-page break below.
    """
    items: list[dict[str, Any]] = []
    skipped_no_sku = 0
    skipped_variation_parent = 0
    skipped_category_excluded = 0
    skipped_skulist = 0
    skipped_no_price = 0

    for page_number in range(1, LW_PAGE_SAFETY_CAP + 1):
        print(f"\n--- Linnworks page {page_number} ---")
        page_items = _fetch_lw_page(page_number)

        if not page_items:
            print(f"    page {page_number} empty — Linnworks pull complete")
            break
        print(f"    {len(page_items)} item(s) returned")

        for it in page_items:
            # Skip checks ordered cheapest-first.
            sku = (it.get("ItemNumber") or "").strip()
            if not sku:
                skipped_no_sku += 1
                continue

            if sku in EXCLUDED_SKUS:
                skipped_skulist += 1
                continue

            category_lower = (it.get("CategoryName") or "").lower()
            if any(sub in category_lower for sub in EXCLUDED_CATEGORY_SUBSTRINGS):
                skipped_category_excluded += 1
                continue

            if it.get("IsVariationParent"):
                skipped_variation_parent += 1
                continue

            try:
                price = float(it.get("RetailPrice") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0:
                skipped_no_price += 1
                continue
            price_pence = int(round(price * 100))

            barcode = (it.get("BarcodeNumber") or "").strip() or None
            qty = _extract_default_qty(it.get("StockLevels") or [])
            # Linnworks StockItemId — feeds sq_sku_map.linnworks_item_id
            # (uuid column). Trusted as-is; if missing, store None
            # rather than fabricating a value.
            lw_item_id = (it.get("StockItemId") or "").strip() or None

            items.append({
                "sku": sku,
                "name": (it.get("ItemTitle") or "").strip(),
                "price_pence": price_pence,
                "barcode": barcode,
                "qty": qty,
                "linnworks_item_id": lw_item_id,
            })

        if len(page_items) < LW_ENTRIES_PER_PAGE:
            print(
                f"    page {page_number} partial ({len(page_items)} < "
                f"{LW_ENTRIES_PER_PAGE}) — last page, Linnworks pull complete"
            )
            break
    else:
        print(
            f"\n!!! Linnworks page safety cap ({LW_PAGE_SAFETY_CAP}) hit — "
            f"catalog larger than expected, raise the cap !!!"
        )

    print(
        f"\n=== Linnworks pull: {len(items)} item(s) collected, "
        f"{skipped_no_sku} skipped (null/empty SKU), "
        f"{skipped_variation_parent} variation parents skipped, "
        f"{skipped_category_excluded} category-excluded skipped, "
        f"{skipped_skulist} SKU-list skipped, "
        f"{skipped_no_price} no-price skipped ==="
    )
    return items


# ---------- Square catalog walk ----------


def _fetch_sq_page(cursor: Optional[str]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "object_types": ["ITEM"],
        "limit": SQUARE_PAGE_LIMIT,
        "include_related_objects": True,
    }
    if cursor:
        body["cursor"] = cursor
    return square.call("catalog/search", method="POST", json_body=body) or {}


def _walk_square_catalog() -> tuple[dict[str, dict[str, Any]], int]:
    """Returns (square_sku_map, duplicate_sku_count).

    square_sku_map: dict[sku, {item_id, item_version, variation_id,
    variation_version, current_name, current_price_pence, current_qty}]
    where current_qty starts as None (filled in by inventory pre-fetch).
    """
    square_map: dict[str, dict[str, Any]] = {}
    duplicate_skus = 0
    cursor: Optional[str] = None
    pages = 0
    items_walked = 0
    skipped_non_regular = 0

    while True:
        pages += 1
        print(f"\n--- Square page {pages} (cursor={cursor!r}) ---")
        response = _fetch_sq_page(cursor)
        objects = response.get("objects") or []

        for item in objects:
            items_walked += 1
            item_data = item.get("item_data") or {}
            if item_data.get("product_type") != PRODUCT_TYPE_REGULAR:
                skipped_non_regular += 1
                continue
            for var in item_data.get("variations") or []:
                var_data = var.get("item_variation_data") or {}
                sku = (var_data.get("sku") or "").strip()
                if not sku:
                    continue
                price_money = var_data.get("price_money") or {}
                try:
                    price_pence = int(price_money.get("amount") or 0)
                except (TypeError, ValueError):
                    price_pence = 0

                if sku in square_map:
                    duplicate_skus += 1
                    print(
                        f"    [warn] duplicate SKU {sku!r} on item {item.get('id')} — "
                        f"keeping first occurrence ({square_map[sku]['item_id']})"
                    )
                    continue

                square_map[sku] = {
                    "item_id": item.get("id"),
                    "item_version": item.get("version"),
                    "variation_id": var.get("id"),
                    "variation_version": var.get("version"),
                    "current_name": item_data.get("name") or "",
                    "current_price_pence": price_pence,
                    "current_qty": None,  # filled in by inventory fetch
                }

        print(
            f"    {len(objects)} ITEM(s) returned, "
            f"{len(square_map)} REGULAR SKUs in map, "
            f"{skipped_non_regular} non-REGULAR skipped, "
            f"{duplicate_skus} duplicate SKU(s) so far"
        )

        cursor = response.get("cursor")
        if not cursor:
            print(f"    no cursor — Square walk complete after page {pages}")
            break

    print(
        f"\n=== Square walk: {items_walked} ITEM(s) walked, "
        f"{len(square_map)} REGULAR SKUs mapped, "
        f"{duplicate_skus} duplicate SKU(s) ignored ==="
    )
    return square_map, duplicate_skus


def _fetch_current_inventory(square_map: dict[str, dict[str, Any]]) -> None:
    """Mutates square_map in place: sets current_qty on each entry.

    Variations not present in the response default to current_qty=0
    (Square returns no count for variations that have never had stock).
    """
    variation_ids = [v["variation_id"] for v in square_map.values() if v.get("variation_id")]
    if not variation_ids:
        return

    var_to_sku: dict[str, str] = {}
    for sku, entry in square_map.items():
        if entry.get("variation_id"):
            var_to_sku[entry["variation_id"]] = sku

    n_chunks = (len(variation_ids) + INVENTORY_FETCH_CHUNK - 1) // INVENTORY_FETCH_CHUNK
    print(
        f"\n--- fetching current Square inventory for {len(variation_ids)} "
        f"variation(s) in {n_chunks} chunk(s) ---"
    )
    counts_seen: dict[str, int] = {}
    for chunk_idx, i in enumerate(range(0, len(variation_ids), INVENTORY_FETCH_CHUNK), start=1):
        chunk = variation_ids[i:i + INVENTORY_FETCH_CHUNK]
        body = {
            "catalog_object_ids": chunk,
            "location_ids": [SHOP_LOCATION_ID],
        }
        try:
            resp = square.call("inventory/counts/batch-retrieve", method="POST", json_body=body) or {}
        except square.SquareError as e:
            print(f"    chunk {chunk_idx}/{n_chunks} FAILED: {str(e)[:200]}")
            continue
        counts = resp.get("counts") or []
        for c in counts:
            cat_id = c.get("catalog_object_id")
            try:
                qty = int(c.get("quantity") or 0)
            except (TypeError, ValueError):
                qty = 0
            if cat_id:
                counts_seen[cat_id] = qty
        print(f"    chunk {chunk_idx}/{n_chunks}: {len(counts)} count entries")

    # Default unseen variations to 0
    for sku, entry in square_map.items():
        var_id = entry.get("variation_id")
        entry["current_qty"] = counts_seen.get(var_id, 0)


# ---------- classification ----------


def _classify(
    linnworks_items: list[dict[str, Any]],
    square_map: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    creates: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    stock_only: list[dict[str, Any]] = []
    no_op: list[dict[str, Any]] = []

    for it in linnworks_items:
        sku = it["sku"]
        existing = square_map.get(sku)
        if existing is None:
            creates.append(it)
            continue
        name_diff = (existing["current_name"] or "") != (it["name"] or "")
        price_diff = (existing["current_price_pence"] or 0) != (it["price_pence"] or 0)
        if name_diff or price_diff:
            entry = dict(it)
            entry["_existing"] = existing
            entry["_reason"] = (
                ("name " if name_diff else "") + ("price" if price_diff else "")
            ).strip()
            updates.append(entry)
            continue
        if (existing.get("current_qty") or 0) != (it["qty"] or 0):
            entry = dict(it)
            entry["_existing"] = existing
            stock_only.append(entry)
            continue
        no_op.append(it)

    return creates, updates, stock_only, no_op


# ---------- write helpers ----------


def _temp_item_id(sku: str) -> str:
    return f"#temp_{sku}"


def _temp_var_id(sku: str) -> str:
    return f"#var_{sku}"


def _build_variation_data(it: dict[str, Any]) -> dict[str, Any]:
    var_data: dict[str, Any] = {
        "name": "Regular",
        "sku": it["sku"],
        "pricing_type": "FIXED_PRICING",
        "price_money": {"amount": int(it["price_pence"]), "currency": "GBP"},
        "track_inventory": True,
    }
    if it.get("barcode"):
        var_data["upc"] = it["barcode"]
    return var_data


def _build_create_object(it: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _temp_item_id(it["sku"]),
        "type": "ITEM",
        "item_data": {
            "name": it["name"],
            "product_type": PRODUCT_TYPE_REGULAR,
            "variations": [{
                "id": _temp_var_id(it["sku"]),
                "type": "ITEM_VARIATION",
                "item_variation_data": _build_variation_data(it),
            }],
        },
    }


def _build_update_object(it: dict[str, Any]) -> dict[str, Any]:
    existing = it["_existing"]
    return {
        "id": existing["item_id"],
        "type": "ITEM",
        "version": existing.get("item_version"),
        "item_data": {
            "name": it["name"],
            "product_type": PRODUCT_TYPE_REGULAR,
            "variations": [{
                "id": existing["variation_id"],
                "type": "ITEM_VARIATION",
                "version": existing.get("variation_version"),
                "item_variation_data": _build_variation_data(it),
            }],
        },
    }


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _do_upsert_batch(
    objects: list[dict[str, Any]],
    label: str,
) -> tuple[dict[str, str], list[str]]:
    """Returns (id_map, errors) where id_map maps client_object_id →
    real object_id from id_mappings.
    """
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "batches": [{"objects": objects}],
    }
    try:
        resp = square.call("catalog/batch-upsert", method="POST", json_body=body) or {}
    except square.SquareError as e:
        return ({}, [f"{label}: {str(e)[:300]}"])

    id_map: dict[str, str] = {}
    for m in resp.get("id_mappings") or []:
        client = m.get("client_object_id")
        real = m.get("object_id")
        if client and real:
            id_map[client] = real
    return (id_map, [])


def _do_creates(
    creates: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, str]], int, list[str]]:
    """Returns (sku_to_new_ids, fail_count, errors) where
    sku_to_new_ids[sku] = {"item_id": <real Square ITEM id>,
                           "variation_id": <real ITEM_VARIATION id>}
    for each successfully created SKU.

    Failures are at the batch granularity — if a batch errors, all
    items in the batch are counted as failed and they don't get any
    real IDs (so we can't push stock or write a sq_sku_map row for
    them).
    """
    if not creates:
        return ({}, 0, [])

    sku_to_new_ids: dict[str, dict[str, str]] = {}
    fail_count = 0
    errors: list[str] = []

    n_batches = (len(creates) + UPSERT_BATCH_SIZE - 1) // UPSERT_BATCH_SIZE
    print(f"\n--- CREATE: {len(creates)} item(s) in {n_batches} batch(es) ---")
    for i, chunk in enumerate(_chunks(creates, UPSERT_BATCH_SIZE), start=1):
        if i > 1:
            time.sleep(SLEEP_BETWEEN_BATCHES)
        objects = [_build_create_object(it) for it in chunk]
        id_map, batch_errors = _do_upsert_batch(objects, label=f"CREATE batch {i}/{n_batches}")
        if batch_errors:
            fail_count += len(chunk)
            errors.extend(batch_errors)
            print(f"    batch {i}/{n_batches}: FAIL — {batch_errors[0][:200]}")
            continue
        # Map temp #temp_<sku>/#var_<sku> → real item/variation ids.
        # Both must resolve for the SKU to count as fully created.
        resolved = 0
        for it in chunk:
            item_real = id_map.get(_temp_item_id(it["sku"]))
            var_real = id_map.get(_temp_var_id(it["sku"]))
            if item_real and var_real:
                sku_to_new_ids[it["sku"]] = {
                    "item_id": item_real,
                    "variation_id": var_real,
                }
                resolved += 1
        unresolved = len(chunk) - resolved
        if unresolved:
            fail_count += unresolved
            errors.append(
                f"CREATE batch {i}: {unresolved} item(s) returned no id_mapping for item+variation"
            )
        print(f"    batch {i}/{n_batches}: OK — {resolved} id pair(s) resolved, {unresolved} unresolved")

    return (sku_to_new_ids, fail_count, errors)


def _do_updates(updates: list[dict[str, Any]]) -> tuple[set[str], int, list[str]]:
    """Returns (failed_skus, fail_count, errors). fail_count is just
    len(failed_skus) — kept as a separate return for symmetry with the
    other write helpers and to avoid double-computing at call sites.
    """
    if not updates:
        return (set(), 0, [])

    failed_skus: set[str] = set()
    errors: list[str] = []

    n_batches = (len(updates) + UPSERT_BATCH_SIZE - 1) // UPSERT_BATCH_SIZE
    print(f"\n--- UPDATE: {len(updates)} item(s) in {n_batches} batch(es) ---")
    for i, chunk in enumerate(_chunks(updates, UPSERT_BATCH_SIZE), start=1):
        if i > 1:
            time.sleep(SLEEP_BETWEEN_BATCHES)
        objects = [_build_update_object(it) for it in chunk]
        _, batch_errors = _do_upsert_batch(objects, label=f"UPDATE batch {i}/{n_batches}")
        if batch_errors:
            for it in chunk:
                failed_skus.add(it["sku"])
            errors.extend(batch_errors)
            print(f"    batch {i}/{n_batches}: FAIL — {batch_errors[0][:200]}")
        else:
            print(f"    batch {i}/{n_batches}: OK — {len(chunk)} item(s) updated")
    return (failed_skus, len(failed_skus), errors)


def _do_stock_push(
    stock_changes: list[dict[str, Any]],
) -> tuple[set[str], int, list[str]]:
    """stock_changes is a list of {variation_id, qty, sku}. Sends in
    chunks via /inventory/changes/batch-create. Returns
    (failed_skus, fail_count, errors). The SKU on each change is used
    only to attribute per-batch failures back to specific SKUs (so we
    know which sq_sku_map rows are at risk); it isn't sent to Square.
    """
    if not stock_changes:
        return (set(), 0, [])

    failed_skus: set[str] = set()
    errors: list[str] = []
    occurred_at = datetime.now(timezone.utc).isoformat()

    n_batches = (len(stock_changes) + STOCK_BATCH_SIZE - 1) // STOCK_BATCH_SIZE
    print(f"\n--- STOCK push: {len(stock_changes)} change(s) in {n_batches} batch(es) ---")
    for i, chunk in enumerate(_chunks(stock_changes, STOCK_BATCH_SIZE), start=1):
        if i > 1:
            time.sleep(SLEEP_BETWEEN_BATCHES)
        body = {
            "idempotency_key": str(uuid.uuid4()),
            "changes": [
                {
                    "type": "PHYSICAL_COUNT",
                    "physical_count": {
                        "catalog_object_id": c["variation_id"],
                        "location_id": SHOP_LOCATION_ID,
                        "state": "IN_STOCK",
                        "quantity": str(int(c["qty"])),
                        "occurred_at": occurred_at,
                    },
                }
                for c in chunk
            ],
        }
        try:
            square.call("inventory/changes/batch-create", method="POST", json_body=body)
            print(f"    batch {i}/{n_batches}: OK — {len(chunk)} change(s)")
        except square.SquareError as e:
            for c in chunk:
                if c.get("sku"):
                    failed_skus.add(c["sku"])
            errors.append(f"STOCK batch {i}: {str(e)[:300]}")
            print(f"    batch {i}/{n_batches}: FAIL — {str(e)[:200]}")
    return (failed_skus, len(failed_skus), errors)


# ---------- audit ----------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_audit_row(
    *,
    mode: str,
    linnworks_pulled: int,
    square_walked: int,
    created: int,
    updated: int,
    stock_only: int,
    no_op: int,
    failed: int,
    duplicate_skus: int,
    error_messages: list[str],
) -> None:
    summary = ""
    if error_messages:
        summary = " | ".join(m[:100] for m in error_messages[:3])
        if len(error_messages) > 3:
            summary += f" | (+{len(error_messages) - 3} more)"

    payload = {
        "run_at": _now_iso(),
        "mode": mode,
        "linnworks_items_pulled": linnworks_pulled,
        "square_items_walked": square_walked,
        "created": created,
        "updated": updated,
        "stock_only": stock_only,
        "no_op": no_op,
        "failed": failed,
        "duplicate_skus": duplicate_skus,
        "error_summary": summary[:1000] if summary else None,
    }
    try:
        db.client().table("sq_lw_sync_log").insert(payload).execute()
        print("    audit row written to sq_lw_sync_log")
    except Exception as e:
        sys.stderr.write(f"[sq_lw_sync_log insert FAILED] {e}\n")


# ---------- sq_sku_map population ----------


def _sku_map_row(
    *,
    sku: str,
    linnworks_item_id: Optional[str],
    square_catalog_id: Optional[str],
    square_variation_id: Optional[str],
    qty: int,
    price_pence: int,
    now: str,
) -> dict[str, Any]:
    return {
        "sku": sku,
        "linnworks_item_id": linnworks_item_id,
        "square_catalog_id": square_catalog_id,
        "square_variation_id": square_variation_id,
        "last_known_stock": int(qty or 0),
        # numeric column — pass as a 2dp string so the wire format
        # is unambiguous. price_pence/100 in float can round-trip
        # to e.g. 12.989999... .
        "last_known_price": f"{(price_pence or 0) / 100:.2f}",
        "last_pushed_at": now,
        "active": True,
    }


def _build_sku_map_rows(
    *,
    creates: list[dict[str, Any]],
    sku_to_new_ids: dict[str, dict[str, str]],
    updates: list[dict[str, Any]],
    failed_update_skus: set[str],
    stock_only: list[dict[str, Any]],
    failed_stock_skus: set[str],
    no_op: list[dict[str, Any]],
    square_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build sq_sku_map upsert rows.

    Inclusion rules per category:
      - CREATE: catalog upsert succeeded (sku in sku_to_new_ids).
        If the follow-on stock push for this SKU failed, we still
        record the catalog mapping — the next run will see a
        STOCK_ONLY discrepancy and reconcile last_known_stock.
      - UPDATE: catalog upsert succeeded (sku not in
        failed_update_skus).
      - STOCK_ONLY: stock push succeeded (sku not in
        failed_stock_skus). The only operation in this category IS
        the stock push, so failure means there's nothing to record.
      - NO_OP: always — these are the backfill rows for SKUs that
        already match Square and otherwise wouldn't get written.
    """
    now = _now_iso()
    rows: list[dict[str, Any]] = []

    for it in creates:
        ids = sku_to_new_ids.get(it["sku"])
        if not ids:
            continue
        rows.append(_sku_map_row(
            sku=it["sku"],
            linnworks_item_id=it.get("linnworks_item_id"),
            square_catalog_id=ids.get("item_id"),
            square_variation_id=ids.get("variation_id"),
            qty=it["qty"],
            price_pence=it["price_pence"],
            now=now,
        ))

    for it in updates:
        if it["sku"] in failed_update_skus:
            continue
        existing = it.get("_existing") or {}
        rows.append(_sku_map_row(
            sku=it["sku"],
            linnworks_item_id=it.get("linnworks_item_id"),
            square_catalog_id=existing.get("item_id"),
            square_variation_id=existing.get("variation_id"),
            qty=it["qty"],
            price_pence=it["price_pence"],
            now=now,
        ))

    for it in stock_only:
        if it["sku"] in failed_stock_skus:
            continue
        existing = it.get("_existing") or {}
        rows.append(_sku_map_row(
            sku=it["sku"],
            linnworks_item_id=it.get("linnworks_item_id"),
            square_catalog_id=existing.get("item_id"),
            square_variation_id=existing.get("variation_id"),
            qty=it["qty"],
            price_pence=it["price_pence"],
            now=now,
        ))

    for it in no_op:
        existing = square_map.get(it["sku"]) or {}
        rows.append(_sku_map_row(
            sku=it["sku"],
            linnworks_item_id=it.get("linnworks_item_id"),
            square_catalog_id=existing.get("item_id"),
            square_variation_id=existing.get("variation_id"),
            qty=it["qty"],
            price_pence=it["price_pence"],
            now=now,
        ))

    return rows


def _upsert_sku_map(rows: list[dict[str, Any]]) -> int:
    """Batch-upsert into sq_sku_map keyed on sku. Returns the count
    of rows successfully written. Per-batch failures are logged to
    stderr and don't crash the run — sq_sku_map is observability for
    the cron, not the source of truth.
    """
    if not rows:
        return 0
    written = 0
    n_batches = (len(rows) + SKU_MAP_BATCH_SIZE - 1) // SKU_MAP_BATCH_SIZE
    print(f"\n--- sq_sku_map upsert: {len(rows)} row(s) in {n_batches} batch(es) ---")
    for i, chunk in enumerate(_chunks(rows, SKU_MAP_BATCH_SIZE), start=1):
        try:
            db.client().table("sq_sku_map").upsert(chunk, on_conflict="sku").execute()
            written += len(chunk)
            print(f"    batch {i}/{n_batches}: OK — {len(chunk)} row(s) upserted")
        except Exception as e:
            sys.stderr.write(
                f"[sq_sku_map upsert FAILED batch {i}/{n_batches}] {e}\n"
            )
            print(f"    batch {i}/{n_batches}: FAIL — {str(e)[:200]}")
    return written


# ---------- preview ----------


def _print_preview(label: str, items: list[dict[str, Any]]) -> None:
    if not items:
        print(f"\n--- {label}: none ---")
        return
    n_show = min(PREVIEW_PER_CATEGORY, len(items))
    print(f"\n--- {label}: first {n_show} of {len(items)} ---")
    for it in items[:PREVIEW_PER_CATEGORY]:
        existing = it.get("_existing") or {}
        reason = it.get("_reason")
        extra = ""
        if reason:
            extra = f"  reason=[{reason}]"
        elif existing:
            extra = (
                f"  current_qty={existing.get('current_qty')}"
                f" → desired_qty={it['qty']}"
            )
        print(
            f"  - sku={it['sku']!r}  name={it['name']!r}  "
            f"price_pence={it['price_pence']}  qty={it['qty']}{extra}"
        )


# ---------- main ----------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sync_linnworks_to_square",
        description=(
            "Pulls every stock item from Linnworks and reconciles "
            "against Square's REGULAR catalog: creates new items, "
            "updates ones whose name/price drifted, and pushes "
            "current stock at the Northwest Guitars location. "
            "Default mode is OBSERVE (dry run). Pass --write to "
            "actually call Square's write endpoints."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually create/update items and push stock. Without this, observe-only.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N Linnworks items. Useful for staged rollouts and testing.",
    )
    args = parser.parse_args(argv)

    mode = "write" if args.write else "observe"
    print(
        f"\n{'!' * 70}\n"
        f"!!  sync_linnworks_to_square — mode={mode.upper()}\n"
        f"!!  {'will WRITE to Square (catalog + inventory)' if args.write else 'DRY RUN — no Square writes'}\n"
        f"{'!' * 70}\n"
    )

    # ---------- pull ----------
    linnworks_items = _pull_linnworks_items()
    if args.limit is not None:
        if args.limit < 0:
            print(f"ERROR: --limit must be non-negative, got {args.limit}")
            return 2
        if args.limit < len(linnworks_items):
            print(f"\n--- --limit {args.limit} applied: trimming Linnworks set from {len(linnworks_items)} to {args.limit} ---")
            linnworks_items = linnworks_items[:args.limit]

    # ---------- walk ----------
    square_map, duplicate_skus = _walk_square_catalog()
    _fetch_current_inventory(square_map)

    # ---------- classify ----------
    creates, updates, stock_only, no_op = _classify(linnworks_items, square_map)

    # The four action counts reflect *what the plan called for* and are
    # set here, in the plan phase, regardless of mode. The write branch
    # adds `failed` separately — it does not subtract from these counts.
    # Reader convention: created=N failed=M means "N attempts, M didn't
    # land, so N-M succeeded".
    created_count = len(creates)
    updated_count = len(updates)
    stock_only_count = len(stock_only)
    no_op_count = len(no_op)

    print("\n" + "=" * 70)
    print("=== PLAN ===")
    print("=" * 70)
    print(f"Linnworks items pulled (after limit): {len(linnworks_items)}")
    print(f"Square REGULAR SKUs walked:           {len(square_map)} (duplicate SKUs ignored: {duplicate_skus})")
    print(f"Would CREATE:    {created_count}")
    print(f"Would UPDATE:    {updated_count}")
    print(f"Would STOCK_ONLY:{stock_only_count}")
    print(f"Would NO_OP:     {no_op_count}")

    _print_preview("CREATE", creates)
    _print_preview("UPDATE", updates)
    _print_preview("STOCK_ONLY", stock_only)
    _print_preview("NO_OP", no_op)

    # ---------- observe → exit ----------
    if not args.write:
        print(f"\n=== DRY RUN — no Square writes performed. Run with --write to execute. ===")
        _write_audit_row(
            mode=mode,
            linnworks_pulled=len(linnworks_items),
            square_walked=len(square_map),
            created=created_count,
            updated=updated_count,
            stock_only=stock_only_count,
            no_op=no_op_count,
            failed=0,
            duplicate_skus=duplicate_skus,
            error_messages=[],
        )
        print(
            f"\n=== SYNC COMPLETE: created={created_count} "
            f"updated={updated_count} stock_only={stock_only_count} "
            f"no_op={no_op_count} failed=0 duplicate_skus={duplicate_skus} "
            f"sku_map_upserted=0 (observe mode) ==="
        )
        return 0

    # ---------- write ----------
    print(
        f"\n=== WRITE MODE — about to issue Square catalog + inventory writes "
        f"({created_count} create, {updated_count} update, {stock_only_count} stock-only) ==="
    )

    sku_to_new_ids, create_fail, create_errors = _do_creates(creates)
    failed_update_skus, update_fail, update_errors = _do_updates(updates)

    # Build stock changes: CREATE (with new var ids), UPDATE (existing
    # var ids), STOCK_ONLY (existing var ids). Each carries its sku
    # so _do_stock_push can attribute per-batch failures back to
    # specific SKUs for the sq_sku_map upsert.
    stock_changes: list[dict[str, Any]] = []
    for it in creates:
        var_id = (sku_to_new_ids.get(it["sku"]) or {}).get("variation_id")
        if var_id:
            stock_changes.append({"variation_id": var_id, "qty": it["qty"], "sku": it["sku"]})
    for it in updates:
        var_id = (it.get("_existing") or {}).get("variation_id")
        if var_id:
            stock_changes.append({"variation_id": var_id, "qty": it["qty"], "sku": it["sku"]})
    for it in stock_only:
        var_id = (it.get("_existing") or {}).get("variation_id")
        if var_id:
            stock_changes.append({"variation_id": var_id, "qty": it["qty"], "sku": it["sku"]})

    failed_stock_skus, stock_fail, stock_errors = _do_stock_push(stock_changes)

    total_failed = create_fail + update_fail + stock_fail
    all_errors = create_errors + update_errors + stock_errors

    # ---------- sq_sku_map population ----------
    sku_map_rows = _build_sku_map_rows(
        creates=creates,
        sku_to_new_ids=sku_to_new_ids,
        updates=updates,
        failed_update_skus=failed_update_skus,
        stock_only=stock_only,
        failed_stock_skus=failed_stock_skus,
        no_op=no_op,
        square_map=square_map,
    )
    sku_map_rows_upserted = _upsert_sku_map(sku_map_rows)

    _write_audit_row(
        mode=mode,
        linnworks_pulled=len(linnworks_items),
        square_walked=len(square_map),
        created=created_count,
        updated=updated_count,
        stock_only=stock_only_count,
        no_op=no_op_count,
        failed=total_failed,
        duplicate_skus=duplicate_skus,
        error_messages=all_errors,
    )

    print("\n" + "=" * 70)
    print(
        f"=== SYNC COMPLETE: created={created_count} updated={updated_count} "
        f"stock_only={stock_only_count} no_op={no_op_count} "
        f"failed={total_failed} duplicate_skus={duplicate_skus} "
        f"sku_map_upserted={sku_map_rows_upserted} ==="
    )
    print("=" * 70)

    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
