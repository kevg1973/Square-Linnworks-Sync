"""tests/test_failed_orders_retry.py — proof tests for the failed-orders
retry system.

Background: the order-pull watermark advances to max(updated_at) of the
*successful* orders. So if one order in a batch fails while newer orders
succeed, the watermark drags past the failure and it's silently stranded
forever (observed live: dCxw8888… lost this way). The fix is the
sq_orders_failed retry table, which tracks failures independently of the
watermark and re-attempts them every run until success — or escalation to
`stuck` (+ a one-time email) after MAX_RETRY_ATTEMPTS.

This file proves:
  - first failure on a new order → row with attempts = 1, stuck = FALSE
  - a repeat failure increments attempts (no duplicate row)
  - the MAX_RETRY_ATTEMPTS-th failure flips stuck = TRUE
  - a success after a failure deletes the row
  - stuck rows are excluded from the retry pass
  - the stuck transition fires exactly one email, with the right payload
  - a missing RESEND_API_KEY logs a warning and does not crash

Run directly: `python3 tests/test_failed_orders_retry.py`
(exit 0 = pass, 1 = fail). No network / credentials needed — the `lib.*`
modules are stubbed in sys.modules before the tool is imported (same
pattern as test_merge_duplicate_skus.py), the Supabase client is replaced
with an in-memory fake, and the Resend HTTP call is monkeypatched.
"""

import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# --- lib stubs (see test_merge_duplicate_skus.py for the rationale) -------
_lib = types.ModuleType("lib")
_lib.__path__ = []  # mark as a package
sys.modules["lib"] = _lib
for _sub in ("db", "linnworks", "square"):
    _m = types.ModuleType(f"lib.{_sub}")
    sys.modules[f"lib.{_sub}"] = _m
    setattr(_lib, _sub, _m)

from tools import pull_square_orders_to_linnworks as pull  # noqa: E402


# --- in-memory fake Supabase client ---------------------------------------
# Supports the narrow chain the retry helpers use against sq_orders_failed:
#   table(t).select(...).eq(c, v).limit(n).execute()
#   table(t).insert(row).execute()
#   table(t).update(row).eq(c, v).execute()
#   table(t).delete().eq(c, v).execute()
# Rows live in a dict keyed by square_order_id.


class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, store, table):
        self._store = store
        self._table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._limit = None

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def insert(self, row):
        self._op = "insert"
        self._payload = row
        return self

    def update(self, row):
        self._op = "update"
        self._payload = row
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, row):
        return all(row.get(c) == v for c, v in self._filters)

    def execute(self):
        rows = self._store.setdefault(self._table, {})
        if self._op == "insert":
            key = self._payload["square_order_id"]
            rows[key] = dict(self._payload)
            return _FakeResp([dict(self._payload)])
        if self._op == "select":
            out = [dict(r) for r in rows.values() if self._match(r)]
            if self._limit is not None:
                out = out[: self._limit]
            return _FakeResp(out)
        if self._op == "update":
            changed = []
            for r in rows.values():
                if self._match(r):
                    r.update(self._payload)
                    changed.append(dict(r))
            return _FakeResp(changed)
        if self._op == "delete":
            to_del = [k for k, r in rows.items() if self._match(r)]
            for k in to_del:
                del rows[k]
            return _FakeResp([])
        return _FakeResp([])


class _FakeClient:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeQuery(self.store, name)


def _install_fake_db():
    """Point pull.db.client at a fresh in-memory client. Returns it so the
    test can inspect the backing store directly."""
    fake = _FakeClient()
    pull.db.client = lambda: fake
    return fake


def _failed_rows(fake):
    return fake.store.get("sq_orders_failed", {})


def _make_order(sq_id="ORDER_1"):
    return {
        "id": sq_id,
        "created_at": "2026-06-10T09:00:00Z",
        "updated_at": "2026-06-10T09:00:00Z",
        "line_items": [
            {"catalog_object_id": "VAR_X", "name": "Thing", "quantity": "1",
             "total_money": {"amount": 1000, "currency": "GBP"}}
        ],
    }


# --- fake Resend transport -------------------------------------------------


class _FakePostResp:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class _PostRecorder:
    """Stands in for requests.post — records every call instead of sending."""

    def __init__(self, status_code=200):
        self.calls = []
        self.status_code = status_code

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakePostResp(self.status_code)


# --- tests -----------------------------------------------------------------


def test_first_failure_creates_row() -> None:
    fake = _install_fake_db()
    pull._record_failed_order("ORDER_1", "boom", _make_order("ORDER_1"))

    rows = _failed_rows(fake)
    assert "ORDER_1" in rows, rows
    row = rows["ORDER_1"]
    assert row["attempts"] == 1, row
    assert row["stuck"] is False, row
    assert row["last_error"] == "boom", row
    # The canonical Square JSON is captured on first failure.
    assert row["square_order_json"]["id"] == "ORDER_1", row


def test_second_failure_increments_no_duplicate() -> None:
    fake = _install_fake_db()
    pull._record_failed_order("ORDER_1", "boom-1", _make_order("ORDER_1"))
    pull._record_failed_order("ORDER_1", "boom-2", _make_order("ORDER_1"))

    rows = _failed_rows(fake)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}: {rows!r}"
    row = rows["ORDER_1"]
    assert row["attempts"] == 2, row
    assert row["stuck"] is False, row
    assert row["last_error"] == "boom-2", row


def test_fifth_failure_sets_stuck() -> None:
    fake = _install_fake_db()
    os.environ["RESEND_API_KEY"] = "test-key"
    pull.requests.post = _PostRecorder()  # absorb the escalation email
    try:
        for i in range(pull.MAX_RETRY_ATTEMPTS):
            pull._record_failed_order("ORDER_1", f"boom-{i}", _make_order("ORDER_1"))
    finally:
        os.environ.pop("RESEND_API_KEY", None)

    row = _failed_rows(fake)["ORDER_1"]
    assert row["attempts"] == pull.MAX_RETRY_ATTEMPTS, row
    assert row["stuck"] is True, row


def test_success_deletes_row() -> None:
    fake = _install_fake_db()
    pull._record_failed_order("ORDER_1", "boom", _make_order("ORDER_1"))
    assert "ORDER_1" in _failed_rows(fake)

    pull._clear_failed_order("ORDER_1")
    assert "ORDER_1" not in _failed_rows(fake), _failed_rows(fake)


def test_stuck_rows_excluded_from_retry_pass() -> None:
    fake = _install_fake_db()
    # One non-stuck failure (eligible for retry) and one stuck (not).
    pull._record_failed_order("LIVE", "boom", _make_order("LIVE"))
    fake.store["sq_orders_failed"]["STUCK"] = {
        "square_order_id": "STUCK",
        "attempts": 5,
        "stuck": True,
        "square_order_json": _make_order("STUCK"),
    }

    retry = pull._load_retry_orders()
    ids = {o["id"] for o in retry}
    assert ids == {"LIVE"}, f"stuck order leaked into retry pass: {ids!r}"

    assert pull._count_stuck_orders() == 1


def test_stuck_transition_sends_exactly_one_email() -> None:
    fake = _install_fake_db()
    os.environ["RESEND_API_KEY"] = "test-key"
    recorder = _PostRecorder()
    pull.requests.post = recorder
    try:
        # Five failures → escalation on the fifth. A sixth must NOT re-send.
        for i in range(pull.MAX_RETRY_ATTEMPTS + 1):
            pull._record_failed_order("ORDER_1", f"boom-{i}", _make_order("ORDER_1"))
    finally:
        os.environ.pop("RESEND_API_KEY", None)

    assert len(recorder.calls) == 1, (
        f"expected exactly one email, got {len(recorder.calls)}"
    )
    call = recorder.calls[0]
    assert call["url"] == pull.RESEND_API_URL, call["url"]
    body = call["json"]
    assert body["from"] == pull.STUCK_EMAIL_FROM, body
    assert body["to"] == [pull.STUCK_EMAIL_TO], body
    assert "ORDER_1" in body["subject"], body["subject"]
    assert "ORDER_1" in body["text"], body["text"]
    # Auth header carries the key as a Bearer token.
    assert call["headers"]["Authorization"] == "Bearer test-key", call["headers"]
    # On a successful send the row is stamped so we never re-notify.
    assert _failed_rows(fake)["ORDER_1"]["stuck_notified_at"] is not None


def test_missing_resend_key_does_not_crash() -> None:
    fake = _install_fake_db()
    os.environ.pop("RESEND_API_KEY", None)
    recorder = _PostRecorder()
    pull.requests.post = recorder

    # Drive a full escalation with no key set. Must not raise; no HTTP call.
    for i in range(pull.MAX_RETRY_ATTEMPTS):
        pull._record_failed_order("ORDER_1", f"boom-{i}", _make_order("ORDER_1"))

    assert len(recorder.calls) == 0, "no email should be sent without a key"
    row = _failed_rows(fake)["ORDER_1"]
    assert row["stuck"] is True, row
    # Email never sent → stamp stays NULL so the gap is visible later.
    assert row.get("stuck_notified_at") is None, row

    # Direct call also returns False rather than raising.
    assert pull._send_stuck_order_email({"square_order_id": "X"}) is False


def main() -> int:
    tests = [
        test_first_failure_creates_row,
        test_second_failure_increments_no_duplicate,
        test_fifth_failure_sets_stuck,
        test_success_deletes_row,
        test_stuck_rows_excluded_from_retry_pass,
        test_stuck_transition_sends_exactly_one_email,
        test_missing_resend_key_does_not_crash,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{'PASS' if not failures else 'FAIL'}: "
          f"{len(tests) - failures}/{len(tests)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
