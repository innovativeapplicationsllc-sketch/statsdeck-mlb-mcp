#!/usr/bin/env python
"""
Export usage_events to a clean CSV and/or JSON file for offline analysis.

Reads DATABASE_URL from the environment (never hardcoded). Supports optional
date-range and per-user filtering.

Examples
--------
    # Everything, to usage_export.csv
    python scripts/export_usage.py

    # A date window (inclusive), CSV
    python scripts/export_usage.py --start 2026-06-01 --end 2026-06-11

    # One user, JSON
    python scripts/export_usage.py --user user_2abc... --format json --out alice.json

    # Both CSV and JSON, only tool_call events
    python scripts/export_usage.py --event-type tool_call --format both --out june
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

COLUMNS = [
    "id", "created_at", "user_id", "event_type", "tool_name", "prompt_name",
    "sport", "params", "success", "error_type", "latency_ms", "cache_hit",
]


def _build_query(args) -> "tuple[str, list]":
    where: "list[str]" = []
    params: "list" = []
    if args.start:
        where.append("created_at >= %s")
        params.append(args.start)
    if args.end:
        # inclusive end-of-day
        where.append("created_at < (%s::date + INTERVAL '1 day')")
        params.append(args.end)
    if args.user:
        where.append("user_id = %s")
        params.append(args.user)
    if args.event_type:
        where.append("event_type = %s")
        params.append(args.event_type)
    if args.tool:
        where.append("tool_name = %s")
        params.append(args.tool)

    sql = f"SELECT {', '.join(COLUMNS)} FROM usage_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at ASC"
    if args.limit:
        sql += " LIMIT %s"
        params.append(args.limit)
    return sql, params


def _normalize(row: dict) -> dict:
    out = dict(row)
    ca = out.get("created_at")
    if isinstance(ca, datetime):
        out["created_at"] = ca.astimezone(timezone.utc).isoformat()
    return out


def _write_csv(rows: "list[dict]", path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for r in rows:
            r = _normalize(r)
            if isinstance(r.get("params"), (dict, list)):
                r["params"] = json.dumps(r["params"])
            writer.writerow(r)


def _write_json(rows: "list[dict]", path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([_normalize(r) for r in rows], f, indent=2, default=str)


def main() -> int:
    ap = argparse.ArgumentParser(description="Export StatsDeck usage_events to CSV/JSON.")
    ap.add_argument("--start", help="Start date YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", help="End date YYYY-MM-DD (inclusive)")
    ap.add_argument("--user", help="Filter by Clerk user_id")
    ap.add_argument("--event-type", dest="event_type",
                    help="Filter by event_type (tool_call | prompt_used)")
    ap.add_argument("--tool", help="Filter by tool_name")
    ap.add_argument("--limit", type=int, help="Max rows")
    ap.add_argument("--format", choices=["csv", "json", "both"], default="csv")
    ap.add_argument("--out", default="usage_export",
                    help="Output path or basename (extension added per format). Default: usage_export")
    args = ap.parse_args()

    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        return 2

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError:
        print("ERROR: psycopg not installed. Run: pip install 'psycopg[binary]'", file=sys.stderr)
        return 3

    sql, params = _build_query(args)
    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    base = args.out
    for ext in (".csv", ".json"):
        if base.endswith(ext):
            base = base[: -len(ext)]

    written = []
    if args.format in ("csv", "both"):
        path = base + ".csv"
        _write_csv(rows, path)
        written.append(path)
    if args.format in ("json", "both"):
        path = base + ".json"
        _write_json(rows, path)
        written.append(path)

    print(f"Exported {len(rows)} event(s) to: {', '.join(written)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
