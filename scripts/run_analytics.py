#!/usr/bin/env python
"""
Run the ready-made analytics queries from analytics_queries.sql with readable,
aligned table output. Single source of truth: the .sql file next to this script.

Usage
-----
    python scripts/run_analytics.py --list                 # show query names
    python scripts/run_analytics.py dau                    # daily active users
    python scripts/run_analytics.py wau
    python scripts/run_analytics.py calls-per-user
    python scripts/run_analytics.py tool-popularity
    python scripts/run_analytics.py error-rate
    python scripts/run_analytics.py cache-hit-rate
    python scripts/run_analytics.py user-history --user user_2abc...
    python scripts/run_analytics.py all                    # every query except user-history

Reads DATABASE_URL from the environment (never hardcoded).
"""

import argparse
import os
import re
import sys
from pathlib import Path

SQL_FILE = Path(__file__).parent / "analytics_queries.sql"
_NAME_RE = re.compile(r"^--\s*name:\s*([a-z0-9-]+)", re.IGNORECASE)


def load_queries() -> "dict[str, str]":
    """Parse the .sql file into {name: sql} using the '-- name: x' markers."""
    queries: "dict[str, str]" = {}
    current = None
    buf: "list[str]" = []
    for line in SQL_FILE.read_text().splitlines():
        m = _NAME_RE.match(line.strip())
        if m:
            if current:
                queries[current] = _clean("\n".join(buf))
            current = m.group(1).lower()
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current:
        queries[current] = _clean("\n".join(buf))
    return queries


def _clean(sql: str) -> str:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines).strip()


def _print_table(columns, rows) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = list(columns)
    widths = [len(str(c)) for c in cols]
    str_rows = []
    for r in rows:
        cells = ["" if v is None else str(v) for v in r]
        str_rows.append(cells)
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))
    header = "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cols))
    print(header)
    print("  ".join("-" * widths[i] for i in range(len(cols))))
    for cells in str_rows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)))
    print(f"\n({len(rows)} row{'s' if len(rows) != 1 else ''})")


def main() -> int:
    queries = load_queries()
    ap = argparse.ArgumentParser(description="Run StatsDeck analytics queries.")
    ap.add_argument("query", nargs="?", help="Query name, or 'all'")
    ap.add_argument("--user", help="user_id (required for user-history)")
    ap.add_argument("--list", action="store_true", help="List available query names")
    args = ap.parse_args()

    if args.list or not args.query:
        print("Available queries:")
        for name in queries:
            print(f"  {name}")
        print("\nRun one with: python scripts/run_analytics.py <name>")
        return 0

    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        return 2
    try:
        import psycopg
    except ModuleNotFoundError:
        print("ERROR: psycopg not installed. Run: pip install 'psycopg[binary]'", file=sys.stderr)
        return 3

    if args.query == "all":
        to_run = [(n, q) for n, q in queries.items() if n != "user-history"]
    else:
        if args.query not in queries:
            print(f"Unknown query '{args.query}'. Use --list to see options.", file=sys.stderr)
            return 4
        to_run = [(args.query, queries[args.query])]

    with psycopg.connect(db_url, connect_timeout=15) as conn:
        for name, sql in to_run:
            params = None
            if ":user" in sql:
                if not args.user:
                    print(f"Query '{name}' needs --user <user_id>.", file=sys.stderr)
                    return 5
                sql = sql.replace(":user", "%s")
                params = [args.user]
            print(f"\n=== {name} ===")
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchall() if cur.description else []
                _print_table(cols, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
