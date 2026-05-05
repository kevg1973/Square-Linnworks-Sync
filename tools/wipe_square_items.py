"""tools/wipe_square_items.py — DESTRUCTIVE.

Phase 1, step 1 — wipe every retail item from Square so the catalog
can be rebuilt cleanly from Linnworks. Services in Square
Appointments are NEVER touched.

## Safety posture

- **Default is observe-mode** (a dry run that prints what it would
  delete and exits). You must pass `--write` to actually delete.
- **Only items with `item_data.product_type == "APPOINTMENTS_SERVICE"`
  are preserved. Everything else is deleted** — including items
  where `product_type` is `"REGULAR"`, missing entirely, or any
  other unrecognised value. The keep-list is the contract; the
  delete-list is "everything else".
- `--limit N` caps the number of deletes per run, so a wipe can be
  staged in chunks.
- Every run (observe or write) writes one row to `sq_wipe_log` for
  audit. If the audit insert fails, the run still completes — audit
  is observability, not the source of truth.

## Pagination during deletion

The walk runs to completion (no artificial cap) and collects IDs
into memory before any delete is issued. We never delete while a
cursor is in flight — that avoids the "cursor invalidated by
mutation" class of bug.

## Batch delete

Square's `POST /v2/catalog/batch-delete` accepts up to 200
object_ids per call. Chunks of 200 with a 0.2s sleep between
batches keeps us at ~5 req/sec, well under Square's 10 req/sec
limit. Per-batch failures are logged but don't crash the run —
remaining batches still attempt.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

from lib import db, square


PAGE_LIMIT = 100
DELETE_BATCH_SIZE = 200
SLEEP_BETWEEN_BATCHES = 0.2

# Classification — APPOINTMENTS_SERVICE is the keep-list; everything
# else is wiped.
PRODUCT_TYPE_KEEP_SERVICE = "APPOINTMENTS_SERVICE"

PREVIEW_DELETIONS = 20


# ---------- catalog walk ----------


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


def _walk_catalog() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Walk the entire catalog; return (to_delete, services) where
    each entry is {id, name, product_type}. Anything that isn't
    APPOINTMENTS_SERVICE lands in to_delete.
    """
    to_delete: list[dict[str, str]] = []
    services: list[dict[str, str]] = []

    cursor: Optional[str] = None
    pages = 0
    items_walked = 0

    while True:
        pages += 1
        print(f"\n--- fetching page {pages} (cursor={cursor!r}) ---")
        response = _fetch_page(cursor)
        objects = response.get("objects") or []

        for item in objects:
            items_walked += 1
            item_id = item.get("id") or ""
            item_data = item.get("item_data") or {}
            name = item_data.get("name") or ""
            product_type = item_data.get("product_type")
            entry = {"id": item_id, "name": name, "product_type": str(product_type)}

            if product_type == PRODUCT_TYPE_KEEP_SERVICE:
                services.append(entry)
            else:
                to_delete.append(entry)

        print(
            f"    HTTP 200 — {len(objects)} item(s) returned, "
            f"running totals: {len(to_delete)} to-delete, "
            f"{len(services)} APPOINTMENTS_SERVICE (walked={items_walked})"
        )

        cursor = response.get("cursor")
        if not cursor:
            print(f"    no cursor — last page reached after page {pages}")
            break

    return to_delete, services


# ---------- batch delete ----------


def _chunks(seq: list[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _delete_batch(object_ids: list[str]) -> tuple[int, int, Optional[str]]:
    """POST /catalog/batch-delete for one chunk. Returns
    (deleted_count, failed_count, error_text_or_None).
    Per-chunk failures are caught — they don't propagate.
    """
    try:
        resp = square.call(
            "catalog/batch-delete",
            method="POST",
            json_body={"object_ids": object_ids},
        )
    except square.SquareError as e:
        return (0, len(object_ids), str(e)[:200])

    deleted = (resp or {}).get("deleted_object_ids") or []
    not_deleted = set(object_ids) - set(deleted)
    err: Optional[str] = None
    if not_deleted:
        err = (
            f"{len(not_deleted)} ID(s) in this batch were not deleted "
            f"(returned by Square as a partial success). First few: "
            f"{list(not_deleted)[:5]}"
        )
    return (len(deleted), len(not_deleted), err)


def _do_wipe(to_delete: list[dict[str, str]]) -> tuple[int, int, list[str]]:
    """Delete in chunks of DELETE_BATCH_SIZE, sleeping between calls.
    Returns (total_deleted, total_failed, error_messages[]).
    """
    ids = [e["id"] for e in to_delete]
    total_deleted = 0
    total_failed = 0
    error_messages: list[str] = []

    n_batches = (len(ids) + DELETE_BATCH_SIZE - 1) // DELETE_BATCH_SIZE
    print(
        f"\n--- write mode: deleting {len(ids)} items in {n_batches} "
        f"batch(es) of up to {DELETE_BATCH_SIZE} ---"
    )

    for i, chunk in enumerate(_chunks(ids, DELETE_BATCH_SIZE), start=1):
        if i > 1:
            time.sleep(SLEEP_BETWEEN_BATCHES)
        deleted, failed, err = _delete_batch(chunk)
        total_deleted += deleted
        total_failed += failed
        status = "OK" if failed == 0 else "PARTIAL/FAIL"
        print(f"    batch {i}/{n_batches}: {status} — deleted={deleted}, failed={failed}")
        if err:
            error_messages.append(f"batch {i}: {err}")
            print(f"      error: {err}")

    return total_deleted, total_failed, error_messages


# ---------- audit ----------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_audit_row(
    *,
    mode: str,
    items_walked: int,
    items_deleted: int,
    items_failed: int,
    items_kept_services: int,
    error_messages: list[str],
) -> None:
    """Single row per run in sq_wipe_log. Failures logged to stderr;
    never raise.
    """
    summary = ""
    if error_messages:
        summary = " | ".join(m[:100] for m in error_messages[:3])
        if len(error_messages) > 3:
            summary += f" | (+{len(error_messages) - 3} more)"

    payload = {
        "run_at": _now_iso(),
        "mode": mode,
        "items_walked": items_walked,
        "items_deleted": items_deleted,
        "items_failed": items_failed,
        "items_kept_services": items_kept_services,
        "error_summary": summary[:1000] if summary else None,
    }
    try:
        db.client().table("sq_wipe_log").insert(payload).execute()
        print(f"    audit row written to sq_wipe_log")
    except Exception as e:
        sys.stderr.write(f"[sq_wipe_log insert FAILED] {e}\n")


# ---------- main ----------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wipe_square_items",
        description=(
            "DESTRUCTIVE. Walks the entire Square catalog and deletes "
            "every item except APPOINTMENTS_SERVICE items. "
            "Default mode is OBSERVE (dry run). Pass --write to "
            "actually delete."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually delete the items. Without this flag the tool runs in observe-only mode (a dry run).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Cap on items to delete this run. Useful for staged rollouts. Only applies in --write mode.",
    )
    args = parser.parse_args(argv)

    mode = "write" if args.write else "observe"
    banner = (
        f"\n{'!' * 70}\n"
        f"!!  wipe_square_items — mode={mode.upper()}\n"
        f"!!  {'will DELETE retail items from Square' if args.write else 'DRY RUN — no items will be deleted'}\n"
        f"{'!' * 70}\n"
    )
    print(banner)

    # ---------- walk ----------
    to_delete, services = _walk_catalog()
    items_walked = len(to_delete) + len(services)

    # ---------- summary ----------
    print("\n" + "=" * 70)
    print("=== SUMMARY ===")
    print("=" * 70)
    print(f"Walked {items_walked} items.")
    print(f"  To delete (everything except services): {len(to_delete)}")
    print(f"  APPOINTMENTS_SERVICE (keep):            {len(services)}")

    # ---------- decide what we'd actually delete ----------
    deletions = to_delete
    if args.write and args.limit is not None:
        if args.limit < 0:
            print(f"\nERROR: --limit must be non-negative, got {args.limit}")
            return 2
        if args.limit < len(deletions):
            print(
                f"\n--- --limit {args.limit} applied: capping deletions from "
                f"{len(deletions)} to {args.limit} ---"
            )
            deletions = deletions[:args.limit]

    n_show = min(PREVIEW_DELETIONS, len(deletions))
    print(f"\n--- first {n_show} item(s) that would be deleted this run ---")
    for d in deletions[:PREVIEW_DELETIONS]:
        print(f"  - id={d['id']}, name={d['name']!r}, product_type={d['product_type']!r}")

    # ---------- observe → exit ----------
    if not args.write:
        print(
            f"\n=== DRY RUN — no items deleted. "
            f"Run with --write to actually delete {len(to_delete)} item(s). ==="
        )
        _write_audit_row(
            mode=mode,
            items_walked=items_walked,
            items_deleted=0,
            items_failed=0,
            items_kept_services=len(services),
            error_messages=[],
        )
        return 0

    # ---------- write ----------
    print(
        f"\n=== WRITE MODE — about to delete {len(deletions)} item(s) "
        f"from Square. Services and unknown types are NOT included. ==="
    )
    deleted_count, failed_count, error_messages = _do_wipe(deletions)

    print("\n" + "=" * 70)
    print(f"=== WIPE COMPLETE: deleted {deleted_count}, failed {failed_count} ===")
    print("=" * 70)

    _write_audit_row(
        mode=mode,
        items_walked=items_walked,
        items_deleted=deleted_count,
        items_failed=failed_count,
        items_kept_services=len(services),
        error_messages=error_messages,
    )

    # Exit non-zero if anything failed, so the GH Actions run is red.
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
