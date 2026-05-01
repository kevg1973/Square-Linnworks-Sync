"""Square API client.

We're using a personal access token directly (Bearer auth in the
Authorization header). No OAuth refresh flow — these tokens don't
expire. If the smoke test ever returns AUTHENTICATION_ERROR /
UNAUTHORIZED in production, that means the token was either revoked
or we're accidentally holding an OAuth token (which does expire) —
flag it loudly rather than silently retrying.

Square API base: https://connect.squareup.com/v2/
We pin a Square-Version header for stability; bump it deliberately.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

from . import config

BASE_URL = "https://connect.squareup.com/v2"

# Pin a known-good API version. When upgrading, test against the
# changelog first: https://developer.squareup.com/docs/changelog
SQUARE_VERSION = "2025-04-16"

# Square's per-endpoint rate limits are generous (10 req/sec on most),
# but we still throttle modestly during heavy operations.
_RATE_LIMIT_SLEEP = 0.15


class SquareError(RuntimeError):
    """Raised when Square returns a non-2xx response."""


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.SQUARE_ACCESS_TOKEN}",
        "Square-Version": SQUARE_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def call(
    path: str,
    *,
    method: str = "GET",
    json_body: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
    timeout: int = 60,
    rate_limit: bool = True,
) -> Any:
    """Make an authenticated call to the Square API.

    Args:
        path: e.g. "locations" — no leading slash, no /v2 prefix
        method: "GET", "POST", "PUT", "DELETE"
        json_body: request body (POST / PUT)
        params: querystring (GET)
        timeout: seconds
        rate_limit: if True, sleeps briefly before the call

    Returns:
        Parsed JSON response.

    Raises:
        SquareError on non-2xx, including the Square error envelope
        text in the message for easier debugging.
    """
    if rate_limit:
        time.sleep(_RATE_LIMIT_SLEEP)

    url = f"{BASE_URL}/{path.lstrip('/')}"
    headers = _headers()

    method = method.upper()
    if method == "GET":
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    elif method == "POST":
        resp = requests.post(url, headers=headers, json=json_body, params=params, timeout=timeout)
    elif method == "PUT":
        resp = requests.put(url, headers=headers, json=json_body, params=params, timeout=timeout)
    elif method == "DELETE":
        resp = requests.delete(url, headers=headers, params=params, timeout=timeout)
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")

    if not resp.ok:
        # Square returns errors as {"errors": [{"category": ..., "code": ..., "detail": ...}]}
        try:
            err = resp.json()
        except ValueError:
            err = {"raw": resp.text[:500]}
        raise SquareError(
            f"Square {method} {path} failed: HTTP {resp.status_code} — {err}"
        )

    if not resp.content:
        return None
    return resp.json()


def smoke_test() -> dict[str, Any]:
    """List locations. This is the canonical "is auth working" call.

    Returns a dict with location count and a short preview, suitable
    for logging in the smoke-test workflow. Does not write anything.
    """
    body = call("locations", method="GET")
    locations = body.get("locations", []) if body else []
    return {
        "authenticated": True,
        "location_count": len(locations),
        "location_names": [loc.get("name") for loc in locations],
        "location_ids": [loc.get("id") for loc in locations],
    }
