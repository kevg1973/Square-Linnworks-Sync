"""Supabase client + audit-log helpers.

Uses the supabase-py SDK with the service-role key (server-side, full
DB access). All this project's tables are prefixed `sq_` to keep them
isolated from the host project they're tacked onto.

Two functions are typically called from every workflow:
    - sync_run_start(job_name) -> run_id
    - sync_run_finish(run_id, status, **counts)

Plus log_error() for non-fatal per-item errors that should still be
surfaced in the dashboard later.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from supabase import Client, create_client

from . import config

_client: Optional[Client] = None


def client() -> Client:
    """Lazy-initialise the Supabase client."""
    global _client
    if _client is None:
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
    return _client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _github_run_url() -> Optional[str]:
    """Construct the GitHub Actions run URL if we're running in CI.

    Returns None for local runs. Lets the dashboard link directly to
    the run logs for any sync_run row.
    """
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not (server and repo and run_id):
        return None
    return f"{server}/{repo}/actions/runs/{run_id}"


def sync_run_start(job_name: str) -> int:
    """Record the start of a sync run. Returns the inserted row's id."""
    resp = (
        client()
        .table("sq_sync_runs")
        .insert(
            {
                "job_name": job_name,
                "started_at": _now_iso(),
                "status": "running",
                "github_run_url": _github_run_url(),
            }
        )
        .execute()
    )
    return resp.data[0]["id"]


def sync_run_finish(
    run_id: int,
    *,
    status: str,
    items_processed: int = 0,
    items_changed: int = 0,
    errors_count: int = 0,
) -> None:
    """Mark a sync run as finished.

    status: 'success' | 'partial' | 'failed'
    """
    if status not in {"success", "partial", "failed"}:
        raise ValueError(f"Invalid status {status!r}")

    (
        client()
        .table("sq_sync_runs")
        .update(
            {
                "finished_at": _now_iso(),
                "status": status,
                "items_processed": items_processed,
                "items_changed": items_changed,
                "errors_count": errors_count,
            }
        )
        .eq("id", run_id)
        .execute()
    )


def log_error(job_name: str, message: str, context: Optional[dict[str, Any]] = None) -> None:
    """Record a non-fatal error. Does not raise — logging must not cascade."""
    try:
        # JSON-encode context defensively; some objects (e.g. requests
        # responses, datetimes) aren't json-serializable by default.
        safe_context = json.loads(json.dumps(context or {}, default=str))
        (
            client()
            .table("sq_errors")
            .insert(
                {
                    "job_name": job_name,
                    "occurred_at": _now_iso(),
                    "message": message[:1000],  # cap to keep rows small
                    "context": safe_context,
                }
            )
            .execute()
        )
    except Exception as e:
        # Fall back to stderr so we at least see something in the GH log.
        import sys

        sys.stderr.write(f"[log_error fallback] {job_name}: {message} ({e})\n")


def get_watermark(key: str) -> Optional[str]:
    """Read a watermark value. Returns None if unset."""
    resp = client().table("sq_watermarks").select("value").eq("key", key).limit(1).execute()
    if not resp.data:
        return None
    return resp.data[0]["value"]


def set_watermark(key: str, value: str) -> None:
    """Upsert a watermark value."""
    (
        client()
        .table("sq_watermarks")
        .upsert(
            {"key": key, "value": value, "updated_at": _now_iso()},
            on_conflict="key",
        )
        .execute()
    )


def smoke_test() -> dict[str, Any]:
    """Round-trip a write+read against sq_sync_runs to confirm DB access.

    Inserts a dummy 'smoke-test' run, immediately marks it finished,
    reads it back, and returns a dict of what was written.
    """
    run_id = sync_run_start("smoke-test")
    sync_run_finish(run_id, status="success", items_processed=0)

    resp = (
        client()
        .table("sq_sync_runs")
        .select("id, job_name, status, started_at, finished_at")
        .eq("id", run_id)
        .single()
        .execute()
    )
    return {"round_trip_ok": True, "row": resp.data}
