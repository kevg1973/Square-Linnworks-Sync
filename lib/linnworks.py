"""Linnworks API client.

Implements the patterns from §2 (auth) and §10 (diagnostic-first) of
LINNWORKS_REFERENCE.md:

- Exchange the install token for a session token + cluster URL.
- Use the cluster URL returned by the auth response — never hardcode.
- Authorization header is the raw session token, no `Bearer` prefix.
- 401 on any call → clear cache, re-auth once, retry once. Persistent
  failure raises.
- Cache the (session_token, server) tuple at module level so multiple
  callers in one run share auth.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

from . import config

AUTH_URL = "https://api.linnworks.net/api/Auth/AuthorizeByApplication"

# Module-level cache. Reset on 401.
_session_token: Optional[str] = None
_server: Optional[str] = None

# Sleep between API calls to stay under the ~1 req/sec rate limit.
_RATE_LIMIT_SLEEP = 1.1


def _authenticate() -> tuple[str, str]:
    """Exchange the install token for a session token + cluster URL.

    Returns (session_token, server_url). Always use the returned
    server URL for subsequent calls — the tenant's cluster is
    determined by the auth response and may differ from any URL we
    might be tempted to hardcode.
    """
    resp = requests.post(
        AUTH_URL,
        data={
            "ApplicationId": config.LINNWORKS_APP_ID,
            "ApplicationSecret": config.LINNWORKS_APP_SECRET,
            "Token": config.LINNWORKS_TOKEN,
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    return body["Token"], body["Server"]


def _ensure_auth() -> tuple[str, str]:
    """Lazy auth. Caches the session token + cluster URL at module level."""
    global _session_token, _server
    if _session_token is None or _server is None:
        _session_token, _server = _authenticate()
    return _session_token, _server


def _clear_auth() -> None:
    """Drop cached auth so the next call re-authenticates."""
    global _session_token, _server
    _session_token = None
    _server = None


def call(
    path: str,
    *,
    method: str = "POST",
    json_body: Optional[dict[str, Any]] = None,
    form_body: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
    timeout: int = 60,
    rate_limit: bool = True,
) -> Any:
    """Make an authenticated call to the Linnworks API.

    Args:
        path: e.g. "Stock/GetStockItems" — no leading slash, no /api prefix
        method: "POST" or "GET"
        json_body: JSON body (most endpoints)
        form_body: form-urlencoded body (auth + Dashboards/ExecuteCustomPagedScript)
        params: querystring (rare on Linnworks)
        timeout: seconds
        rate_limit: if True, sleeps before the call to respect ~1 req/sec

    Returns:
        Parsed JSON response.

    Raises:
        requests.HTTPError on non-401 errors.
        RuntimeError if auth fails twice in a row.
    """
    if rate_limit:
        time.sleep(_RATE_LIMIT_SLEEP)

    token, server = _ensure_auth()
    url = f"{server}/api/{path}"
    headers = {"Authorization": token}

    if form_body is not None:
        # form-urlencoded — used for auth and Dashboards/ExecuteCustomPagedScript
        resp = _send(method, url, headers=headers, data=form_body, timeout=timeout)
    else:
        headers["Content-Type"] = "application/json"
        resp = _send(method, url, headers=headers, json=json_body, params=params, timeout=timeout)

    if resp.status_code == 401:
        # Token expired or revoked. Re-auth once and retry once.
        _clear_auth()
        token, server = _ensure_auth()
        url = f"{server}/api/{path}"
        headers["Authorization"] = token
        if form_body is not None:
            resp = _send(method, url, headers=headers, data=form_body, timeout=timeout)
        else:
            resp = _send(method, url, headers=headers, json=json_body, params=params, timeout=timeout)
        if resp.status_code == 401:
            raise RuntimeError(
                "Linnworks authentication failed twice in a row. "
                "The install token may have been revoked, or the "
                "ApplicationSecret may be wrong."
            )

    if not resp.ok:
        # Surface Linnworks' response body in the error. Without this,
        # callers only see "400 Client Error: Bad Request for url ..."
        # and lose the actual validation message Linnworks returns in
        # the body — the one thing that explains *why* a CreateOrders
        # (or any) call was rejected.
        body = (resp.text or "").strip()[:2000]
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason} for {url} — "
            f"Linnworks response body: {body!r}",
            response=resp,
        )
    if not resp.content:
        return None
    return resp.json()


def _send(method: str, url: str, **kwargs) -> requests.Response:
    """Thin wrapper so we can swap one request style for another above."""
    method = method.upper()
    if method == "GET":
        return requests.get(url, **kwargs)
    return requests.post(url, **kwargs)


def smoke_test() -> dict[str, Any]:
    """Authenticate and return a small dict suitable for logging.

    Used by the smoke-test workflow to confirm credentials are wired
    correctly. Does not make any data calls beyond auth.
    """
    token, server = _ensure_auth()
    return {
        "authenticated": True,
        "server": server,
        "session_token_length": len(token),
    }
