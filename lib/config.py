"""Environment variable loading with loud failures.

Every required variable is read once at import time. If anything is
missing, the process aborts with a clear message naming the missing
variable. This is deliberate: a sync that runs with partial config is
worse than a sync that doesn't run at all.
"""

from __future__ import annotations

import os
import sys


def _required(name: str) -> str:
    """Read an env var; abort if missing or empty."""
    value = os.environ.get(name, "").strip()
    if not value:
        sys.stderr.write(
            f"\nFATAL: required environment variable {name!r} is not set "
            "or is empty.\n"
            "  - In GitHub Actions: add it under repo Settings → "
            "Secrets and variables → Actions.\n"
            "  - Locally: set it in your shell or a .env file you load "
            "before running.\n\n"
        )
        sys.exit(2)
    return value


# Linnworks
LINNWORKS_APP_ID = _required("LINNWORKS_APP_ID")
LINNWORKS_APP_SECRET = _required("LINNWORKS_APP_SECRET")
LINNWORKS_TOKEN = _required("LINNWORKS_TOKEN")

# Square
SQUARE_ACCESS_TOKEN = _required("SQUARE_ACCESS_TOKEN")
SQUARE_APPLICATION_ID = _required("SQUARE_APPLICATION_ID")

# Supabase
SUPABASE_URL = _required("SUPABASE_URL")
SUPABASE_SERVICE_KEY = _required("SUPABASE_SERVICE_KEY")
