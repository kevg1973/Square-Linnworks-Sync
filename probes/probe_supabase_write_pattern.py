"""probes/probe_supabase_write_pattern.py — Phase 0b probe.

Validates the four supabase-py write patterns the production code
will lean on, against the real `sq_*` tables in the host Supabase
project.

Patterns under test:

1. **Upsert with `on_conflict`** on `sq_sku_map`. Stock-push (Phase 2)
   does this every run to keep the spine table fresh.
2. **Batch insert** into `sq_errors` in a single call, with a `jsonb`
   `context` column. Every cron writes per-error rows like this when
   non-fatal failures occur mid-run.
3. **Watermark round-trip** on `sq_watermarks` (read → write → read).
   Order-pull (Phase 3) reads `square_orders_last_pulled_at` at the
   start of every run and writes it at the end.
4. **Filtered query** on `sq_sync_runs` by `status` + `started_at`.
   The dashboard (Phase 4) reads "successful runs in the last N
   hours" via this kind of query.

All test rows are tagged with `__probe_test__` markers so the cleanup
in the `finally` block can reliably purge them. The probe is
re-runnable: a leftover row from a prior failed run gets wiped at
start anyway.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from lib import db


PROBE_SKU = "__PROBE_TEST_DO_NOT_USE__"
PROBE_JOB = "__probe_test__"
PROBE_WATERMARK_KEY = "__probe_test_watermark__"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wipe_probe_rows() -> None:
    """Delete every row created by previous (or current) probe runs.

    Called at start (defensive) and end (cleanup). Best-effort:
    swallows errors so cleanup never cascades.
    """
    c = db.client()
    for table, column, value in [
        ("sq_sku_map",   "sku",      PROBE_SKU),
        ("sq_errors",    "job_name", PROBE_JOB),
        ("sq_watermarks", "key",     PROBE_WATERMARK_KEY),
        ("sq_sync_runs", "job_name", PROBE_JOB),
    ]:
        try:
            c.table(table).delete().eq(column, value).execute()
        except Exception as e:
            print(f"  cleanup warning: {table}/{column}={value!r} → {type(e).__name__}: {e}")


def test_upsert_with_on_conflict() -> bool:
    """Insert, then upsert the same SKU. Confirm the second call
    updates rather than duplicates, and that the new field values won.
    """
    print("\n--- 1. upsert with on_conflict on sq_sku_map ---")
    c = db.client()

    # First insert
    c.table("sq_sku_map").upsert(
        {
            "sku": PROBE_SKU,
            "linnworks_item_id": "00000000-0000-0000-0000-000000000001",
            "last_known_stock": 5,
            "last_known_price": 9.99,
            "active": True,
        },
        on_conflict="sku",
    ).execute()

    # Second upsert — different stock + price, same SKU
    c.table("sq_sku_map").upsert(
        {
            "sku": PROBE_SKU,
            "linnworks_item_id": "00000000-0000-0000-0000-000000000001",
            "last_known_stock": 7,
            "last_known_price": 12.50,
            "active": True,
        },
        on_conflict="sku",
    ).execute()

    rows = (
        c.table("sq_sku_map")
        .select("sku, last_known_stock, last_known_price")
        .eq("sku", PROBE_SKU)
        .execute()
        .data
    )
    print(f"  rows after second upsert: {rows}")
    if len(rows) != 1:
        print(f"=== DISCOVERY: upsert pattern FAILED — expected 1 row, got {len(rows)} ===")
        return False
    if rows[0]["last_known_stock"] != 7 or float(rows[0]["last_known_price"]) != 12.50:
        print(f"=== DISCOVERY: upsert pattern FAILED — values not updated: {rows[0]} ===")
        return False
    print("=== DISCOVERY: upsert with on_conflict='sku' on sq_sku_map works ===")
    return True


def test_batch_insert_with_jsonb() -> bool:
    """Insert three sq_errors rows in one call, with jsonb context.
    Confirm all three landed and the jsonb round-tripped intact.
    """
    print("\n--- 2. batch insert into sq_errors with jsonb context ---")
    c = db.client()

    occurred = _now_iso()
    rows_to_insert = [
        {
            "job_name": PROBE_JOB,
            "occurred_at": occurred,
            "message": f"probe error {i}",
            "context": {
                "iteration": i,
                "nested": {"key": f"value-{i}", "number": i * 10},
                "list": [1, 2, i],
            },
        }
        for i in range(3)
    ]
    c.table("sq_errors").insert(rows_to_insert).execute()

    rows = (
        c.table("sq_errors")
        .select("message, context")
        .eq("job_name", PROBE_JOB)
        .order("message")
        .execute()
        .data
    )
    print(f"  rows after batch insert: {len(rows)}")
    if len(rows) != 3:
        print(f"=== DISCOVERY: batch insert FAILED — expected 3 rows, got {len(rows)} ===")
        return False
    # Spot-check jsonb round-trip on the middle row.
    middle = next((r for r in rows if r["message"] == "probe error 1"), None)
    if not middle:
        print(f"=== DISCOVERY: batch insert FAILED — middle row missing ===")
        return False
    ctx = middle["context"]
    if ctx.get("iteration") != 1 or ctx.get("nested", {}).get("number") != 10:
        print(f"=== DISCOVERY: batch insert FAILED — jsonb round-trip mangled: {ctx} ===")
        return False
    print("=== DISCOVERY: batch insert with jsonb context on sq_errors works ===")
    return True


def test_watermark_round_trip() -> bool:
    """Set, read, overwrite, read again — confirm the watermark
    upserts cleanly.
    """
    print("\n--- 3. watermark round-trip on sq_watermarks ---")

    # Initial set
    initial_value = "2026-05-01T12:00:00+00:00"
    db.set_watermark(PROBE_WATERMARK_KEY, initial_value)
    read_back = db.get_watermark(PROBE_WATERMARK_KEY)
    if read_back != initial_value:
        print(f"=== DISCOVERY: watermark FAILED on first read — wrote {initial_value!r}, "
              f"read {read_back!r} ===")
        return False

    # Overwrite with a different value
    overwrite_value = "2026-05-01T13:30:00+00:00"
    db.set_watermark(PROBE_WATERMARK_KEY, overwrite_value)
    read_back = db.get_watermark(PROBE_WATERMARK_KEY)
    if read_back != overwrite_value:
        print(f"=== DISCOVERY: watermark FAILED on overwrite read — wrote {overwrite_value!r}, "
              f"read {read_back!r} ===")
        return False

    # Confirm there's exactly one row for this key (upsert, not insert).
    c = db.client()
    rows = c.table("sq_watermarks").select("key").eq("key", PROBE_WATERMARK_KEY).execute().data
    if len(rows) != 1:
        print(f"=== DISCOVERY: watermark FAILED — overwrite duplicated rows: count={len(rows)} ===")
        return False

    print("=== DISCOVERY: watermark read/write/overwrite round-trip works ===")
    return True


def test_filtered_query_on_sync_runs() -> bool:
    """Insert a fresh sync_run, then query by status + started_at >= cutoff."""
    print("\n--- 4. filtered query on sq_sync_runs ---")
    c = db.client()

    # Insert a probe run that should match the upcoming filter.
    just_before = datetime.now(timezone.utc) - timedelta(seconds=2)
    insert_resp = (
        c.table("sq_sync_runs")
        .insert(
            {
                "job_name": PROBE_JOB,
                "started_at": _now_iso(),
                "finished_at": _now_iso(),
                "status": "success",
                "items_processed": 42,
            }
        )
        .execute()
    )
    inserted_id = insert_resp.data[0]["id"]
    print(f"  inserted sync_runs id={inserted_id}")

    # Filter: status == success AND started_at >= just_before.
    rows = (
        c.table("sq_sync_runs")
        .select("id, job_name, status, items_processed")
        .eq("status", "success")
        .gte("started_at", just_before.isoformat())
        .eq("job_name", PROBE_JOB)
        .execute()
        .data
    )
    print(f"  filter matched {len(rows)} row(s)")
    if not any(r["id"] == inserted_id for r in rows):
        print(f"=== DISCOVERY: filtered query FAILED — inserted row not in result set ===")
        return False
    if rows[0]["items_processed"] != 42:
        print(f"=== DISCOVERY: filtered query FAILED — column value mismatch: {rows[0]} ===")
        return False
    print("=== DISCOVERY: filtered query (.eq + .gte chain) on sq_sync_runs works ===")
    return True


def main() -> int:
    print("--- probe_supabase_write_pattern ---")

    # Defensive pre-clean — leftover rows from a failed previous run
    # would otherwise skew the assertions.
    print("\n--- pre-clean: removing any leftover __probe_test__ rows ---")
    _wipe_probe_rows()

    results: dict[str, bool] = {}
    try:
        results["upsert_on_conflict"]   = test_upsert_with_on_conflict()
        results["batch_insert_jsonb"]   = test_batch_insert_with_jsonb()
        results["watermark_round_trip"] = test_watermark_round_trip()
        results["filtered_query"]       = test_filtered_query_on_sync_runs()
    finally:
        print("\n--- cleanup: removing test rows ---")
        _wipe_probe_rows()
        # Verify cleanup actually wiped them.
        c = db.client()
        leftovers: list[str] = []
        for table, column, value in [
            ("sq_sku_map",   "sku",      PROBE_SKU),
            ("sq_errors",    "job_name", PROBE_JOB),
            ("sq_watermarks", "key",     PROBE_WATERMARK_KEY),
            ("sq_sync_runs", "job_name", PROBE_JOB),
        ]:
            try:
                count = len(c.table(table).select(column).eq(column, value).execute().data)
                if count:
                    leftovers.append(f"{table} ({count} rows)")
            except Exception:
                pass
        if leftovers:
            print(f"=== DISCOVERY: WARNING — cleanup left rows in: {', '.join(leftovers)}. "
                  "Manually delete by marker before re-running. ===")
        else:
            print("=== DISCOVERY: all test rows cleaned up successfully ===")

    print("\n--- summary ---")
    for name, ok in results.items():
        print(f"  {'OK ' if ok else 'XX '} {name}")

    if all(results.values()):
        print("\n=== DISCOVERY: all four Supabase write patterns work — cleared for production use ===")
        return 0
    failed = [n for n, ok in results.items() if not ok]
    print(f"\n=== DISCOVERY: {len(failed)} pattern(s) FAILED: {', '.join(failed)} ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
