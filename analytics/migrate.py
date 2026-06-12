"""
Create the usage_events table + indexes. Idempotent and reproducible.

Run once after adding the Postgres database (and any time you pull schema changes):

    DATABASE_URL=postgresql://...  python -m analytics.migrate

On Railway you can run it from the service shell, where DATABASE_URL is already set:

    python -m analytics.migrate
"""

import logging
import sys
from pathlib import Path

from . import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("analytics.migrate")

_SCHEMA_FILE = Path(__file__).parent / "schema.sql"


def main() -> int:
    if not config.DATABASE_URL:
        logger.error(
            "DATABASE_URL is not set. On Railway, reference the Postgres database: "
            "set DATABASE_URL = ${{Postgres.DATABASE_URL}} on the StatsDeck service."
        )
        return 2

    try:
        import psycopg
    except ModuleNotFoundError:
        logger.error("psycopg is not installed. Add 'psycopg[binary]' (it's in pyproject.toml).")
        return 3

    ddl = _SCHEMA_FILE.read_text()
    logger.info("Applying schema to %s table from %s", config.TABLE, _SCHEMA_FILE.name)
    with psycopg.connect(config.DATABASE_URL, autocommit=True, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
            cur.execute(
                "SELECT to_regclass(%s)", (config.TABLE,)
            )
            exists = cur.fetchone()[0]
    if exists:
        logger.info("Migration complete — table '%s' is ready.", config.TABLE)
        return 0
    logger.error("Migration ran but table '%s' was not found.", config.TABLE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
