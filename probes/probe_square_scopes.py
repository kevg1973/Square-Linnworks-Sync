"""probes/probe_square_scopes.py — Phase 0b probe.

Verifies that the Square access token in `SQUARE_ACCESS_TOKEN` has
every scope this project will need, by attempting the lightest-touch
call against each endpoint.

For READ scopes, the calls are genuinely read-only and side-effect-
free. For WRITE scopes, the calls send deliberately invalid bodies so
a successful auth/scope check surfaces as a validation error (HTTP
400), while a scope failure surfaces as `INSUFFICIENT_SCOPES`
(HTTP 403). Either way, no data is modified.

Findings are emitted as `=== DISCOVERY: ... ===` lines so they can be
grepped out of the workflow log and pasted into DISCOVERIES.md.

Endpoints checked (with the Square scope each implies):

| Endpoint                                  | Scope               |
|-------------------------------------------|---------------------|
| GET  /v2/locations                        | MERCHANT_PROFILE_READ |
| GET  /v2/catalog/list                     | ITEMS_READ          |
| POST /v2/catalog/upsert-catalog-object    | ITEMS_WRITE         |
| POST /v2/inventory/counts/batch-retrieve  | INVENTORY_READ      |
| POST /v2/inventory/changes/batch-create   | INVENTORY_WRITE     |
| POST /v2/orders/search                    | ORDERS_READ         |
"""

from __future__ import annotations

import sys
from typing import Any, Optional

from lib import square


# Match this against Square's error envelope:
# {"errors": [{"code": "INSUFFICIENT_SCOPES", "category": "...", "detail": "..."}]}
SCOPE_FAILURE_CODE = "INSUFFICIENT_SCOPES"
EXPECTED_NORTHWEST_LOCATION_ID = "L74KSP08AJ2GH"


def _classify_error(exc: square.SquareError) -> tuple[str, str]:
    """Pull (code, detail) out of a SquareError's message string.

    SquareError.__str__ contains the raw error envelope verbatim; we
    just substring-match for the codes we care about. Falls back to
    ("UNKNOWN", "...") if we can't parse a code.
    """
    text = str(exc)
    if SCOPE_FAILURE_CODE in text:
        return (SCOPE_FAILURE_CODE, text)
    if "UNAUTHORIZED" in text or "AUTHENTICATION_ERROR" in text:
        return ("UNAUTHORIZED", text)
    # Look for the first "code": "FOO" pattern.
    marker = '"code":'
    idx = text.find(marker)
    if idx >= 0:
        rest = text[idx + len(marker):].lstrip().lstrip('"')
        end = rest.find('"')
        if end > 0:
            return (rest[:end], text)
    return ("UNKNOWN", text)


def _check(label: str, scope: str, fn) -> tuple[bool, Optional[str]]:
    """Run a single endpoint check.

    Returns (scope_ok, code_if_failed). `scope_ok` is True if the call
    succeeded OR failed with a non-scope error (which still confirms
    the scope is granted — the call only failed because we sent an
    intentionally invalid body).
    """
    try:
        fn()
    except square.SquareError as e:
        code, detail = _classify_error(e)
        if code == SCOPE_FAILURE_CODE:
            print(f"=== DISCOVERY: {scope} scope MISSING on {label} — code: {code} ===")
            print(f"    detail: {detail[:500]}")
            return (False, code)
        if code == "UNAUTHORIZED":
            # The token itself is bad — every probe will fail. Bail loud.
            print(f"=== DISCOVERY: token UNAUTHORIZED on {label} — token is invalid or revoked ===")
            print(f"    detail: {detail[:500]}")
            return (False, code)
        # Any other error (typically 400 INVALID_REQUEST_ERROR for our
        # deliberately-invalid write probes) means the scope check passed.
        print(f"=== DISCOVERY: {scope} scope OK on {label} (validation error: {code}) ===")
        return (True, code)
    print(f"=== DISCOVERY: {scope} scope OK on {label} (call succeeded) ===")
    return (True, None)


def check_locations() -> list[dict[str, Any]]:
    """Lists Square locations. Also confirms the Northwest Guitars
    location is present and prints its ID for cross-checking against
    CLAUDE.md.
    """
    body = square.call("locations", method="GET")
    locations = body.get("locations", []) if body else []
    print(f"=== DISCOVERY: located {len(locations)} Square location(s) ===")
    for loc in locations:
        marker = "  ← Northwest Guitars" if loc.get("id") == EXPECTED_NORTHWEST_LOCATION_ID else ""
        print(f"    • {loc.get('name')!r}  id={loc.get('id')}{marker}")
    if not any(loc.get("id") == EXPECTED_NORTHWEST_LOCATION_ID for loc in locations):
        print(
            f"=== DISCOVERY: WARNING — expected Northwest Guitars location "
            f"{EXPECTED_NORTHWEST_LOCATION_ID} was not in the list. "
            f"Update CLAUDE.md if the ID has changed. ==="
        )
    return locations


def main() -> int:
    print("--- probe_square_scopes ---\n")

    # 1. MERCHANT_PROFILE_READ via GET /v2/locations.
    try:
        locations = check_locations()
    except square.SquareError as e:
        code, detail = _classify_error(e)
        print(f"=== DISCOVERY: locations call FAILED — code: {code} ===")
        print(f"    detail: {detail[:500]}")
        return 1

    # Pick a location_id to use in the orders/search probe. Prefer the
    # known Northwest Guitars id, fall back to whatever's there.
    location_id = next(
        (loc["id"] for loc in locations if loc.get("id") == EXPECTED_NORTHWEST_LOCATION_ID),
        locations[0]["id"] if locations else None,
    )
    if not location_id:
        print("=== DISCOVERY: no Square locations available — cannot run remaining probes ===")
        return 1

    results: dict[str, bool] = {}

    # 2. ITEMS_READ — list one catalog item.
    results["ITEMS_READ"], _ = _check(
        "GET /v2/catalog/list",
        "ITEMS_READ",
        lambda: square.call("catalog/list", method="GET", params={"types": "ITEM"}),
    )

    # 3. ITEMS_WRITE — upsert with a deliberately invalid object so
    #    a scope-OK token surfaces as a validation error, not a write.
    results["ITEMS_WRITE"], _ = _check(
        "POST /v2/catalog/upsert-catalog-object",
        "ITEMS_WRITE",
        lambda: square.call(
            "catalog/object",
            method="POST",
            json_body={
                "idempotency_key": "probe-scope-check-items-write",
                # Empty `object` → guaranteed validation failure if the
                # scope check passes. A scope failure short-circuits before
                # validation, so we can distinguish the two cases.
                "object": {},
            },
        ),
    )

    # 4. INVENTORY_READ — batch-retrieve counts for an obviously
    #    nonexistent id. Returns 200 + empty counts when scope is OK.
    results["INVENTORY_READ"], _ = _check(
        "POST /v2/inventory/counts/batch-retrieve",
        "INVENTORY_READ",
        lambda: square.call(
            "inventory/counts/batch-retrieve",
            method="POST",
            json_body={"catalog_object_ids": ["__probe_nonexistent_id__"]},
        ),
    )

    # 5. INVENTORY_WRITE — batch-create changes with an empty changes
    #    array. Returns a validation error with scope OK.
    results["INVENTORY_WRITE"], _ = _check(
        "POST /v2/inventory/changes/batch-create",
        "INVENTORY_WRITE",
        lambda: square.call(
            "inventory/changes/batch-create",
            method="POST",
            json_body={
                "idempotency_key": "probe-scope-check-inventory-write",
                "changes": [],
            },
        ),
    )

    # 6. ORDERS_READ — search orders, limit 1.
    results["ORDERS_READ"], _ = _check(
        "POST /v2/orders/search",
        "ORDERS_READ",
        lambda: square.call(
            "orders/search",
            method="POST",
            json_body={"location_ids": [location_id], "limit": 1},
        ),
    )

    print("\n--- summary ---")
    for scope, ok in results.items():
        print(f"  {'OK ' if ok else 'XX '} {scope}")

    missing = [scope for scope, ok in results.items() if not ok]
    if missing:
        print(
            f"\n=== DISCOVERY: SCOPES MISSING: {', '.join(missing)} — "
            "re-issue the Square access token with these scopes added "
            "before proceeding to Phase 1+. ==="
        )
        return 2

    print(
        "\n=== DISCOVERY: all six required Square scopes confirmed present — "
        "cleared to proceed to Phase 1 (reconciliation). ==="
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
