"""
Usage-analytics configuration — all read from the environment, nothing hardcoded.

The single switch that turns the whole subsystem on is DATABASE_URL. On Railway,
add a Postgres database to the project and expose its connection string to the
StatsDeck service as a reference variable named DATABASE_URL:

    DATABASE_URL = ${{Postgres.DATABASE_URL}}

If DATABASE_URL is unset (e.g. local stdio dev, CI, tests), analytics is disabled
and every record_event() call is a no-op — tools behave exactly as before.
"""

import os

# Railway exposes the Postgres connection string here when you reference
# ${{Postgres.DATABASE_URL}}. psycopg accepts the postgresql:// URL as-is.
DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()

# Hard opt-out even when DATABASE_URL is present (handy for CI / load tests).
_DISABLED = os.getenv("USAGE_ANALYTICS_DISABLED", "").strip().lower() in (
    "1", "true", "yes", "on",
)

# Master switch. When False, record_event() returns immediately.
ENABLED: bool = bool(DATABASE_URL) and not _DISABLED

# Table name (kept in one place so migrate / writer / exporter agree).
TABLE: str = os.getenv("USAGE_TABLE", "usage_events")

# In-memory buffer between the tool thread and the DB writer thread. If the DB
# is slow/unreachable and this fills up, new events are dropped (never blocked).
QUEUE_MAXSIZE: int = int(os.getenv("USAGE_QUEUE_MAXSIZE", "10000"))
