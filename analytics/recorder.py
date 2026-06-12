"""
Background usage-event recorder.

HARD REQUIREMENTS (do not regress):
  * A tool call must NEVER block, slow, or break because of usage logging.
    record_event() only builds a small dict and drops it on an in-memory queue;
    a daemon thread does all DB I/O off the request path.
  * If the DB is unreachable, DATABASE_URL is unset, or psycopg isn't installed,
    the tool call still succeeds and returns normally. Events are dropped and the
    failure is logged server-side, rate-limited.

The DB write path is isolated in a single daemon thread that owns one connection
and reconnects on failure with backoff. psycopg is imported lazily inside that
thread, so importing this module never requires the driver or a database.
"""

import atexit
import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import config

logger = logging.getLogger("analytics.recorder")

# Cap on serialized params so a pathological argument can't bloat a row.
_PARAMS_MAX_CHARS = 2000

_queue: "queue.Queue[dict]" = queue.Queue(maxsize=config.QUEUE_MAXSIZE)
_worker: "threading.Thread | None" = None
_worker_lock = threading.Lock()
_started = False

# Rate-limit repetitive "queue full" / writer-error logs.
_last_drop_log = 0.0


# ---------------------------------------------------------------------------
# Pluggable sink — the default enqueues for the writer thread; tests swap in a
# synchronous capture so they can assert on the row without a database.
# ---------------------------------------------------------------------------

def _default_sink(row: dict) -> None:
    _ensure_worker()
    try:
        _queue.put_nowait(row)
    except queue.Full:
        global _last_drop_log
        now = time.monotonic()
        if now - _last_drop_log > 30:
            logger.warning("usage queue full (max=%d) — dropping events", config.QUEUE_MAXSIZE)
            _last_drop_log = now


_sink: Callable[[dict], None] = _default_sink


def set_sink(sink: Callable[[dict], None]) -> None:
    """Override where finished rows go (used by tests)."""
    global _sink
    _sink = sink


def reset_sink() -> None:
    global _sink
    _sink = _default_sink


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_event(
    *,
    event_type: str,
    user_id: str,
    tool_name: "str | None" = None,
    prompt_name: "str | None" = None,
    sport: "str | None" = None,
    params: "dict | None" = None,
    success: "bool | None" = None,
    error_type: "str | None" = None,
    latency_ms: "int | None" = None,
    cache_hit: "bool | None" = None,
) -> None:
    """
    Queue one usage event. Disabled (no DATABASE_URL) → immediate no-op.
    This function never raises; any failure is swallowed and logged at debug.
    """
    if not config.ENABLED:
        return
    try:
        row = {
            "created_at": datetime.now(timezone.utc),
            "user_id": user_id or "default",
            "event_type": event_type,
            "tool_name": tool_name,
            "prompt_name": prompt_name,
            "sport": sport,
            "params": _safe_params(params),
            "success": success,
            "error_type": (error_type[:200] if isinstance(error_type, str) else None),
            "latency_ms": latency_ms,
            "cache_hit": cache_hit,
        }
        _sink(row)
    except Exception:  # never let analytics break a tool call
        logger.debug("record_event failed", exc_info=True)


def _safe_params(params: "dict | None") -> "dict | None":
    """Only keep JSON-serializable, size-bounded params. Never store anything huge."""
    if not params:
        return None
    try:
        serialized = json.dumps(params, default=str)
    except Exception:
        return None
    if len(serialized) > _PARAMS_MAX_CHARS:
        return {"_truncated": True, "_keys": list(params.keys())[:25]}
    return params


# ---------------------------------------------------------------------------
# Writer thread
# ---------------------------------------------------------------------------

def _ensure_worker() -> None:
    global _worker, _started
    if _started:
        return
    with _worker_lock:
        if _started:
            return
        _worker = threading.Thread(target=_run, name="usage-writer", daemon=True)
        _worker.start()
        _started = True
        atexit.register(_flush_on_exit)
        logger.info("usage writer thread started")


_INSERT_SQL = (
    "INSERT INTO {table} "
    "(created_at, user_id, event_type, tool_name, prompt_name, sport, params, "
    " success, error_type, latency_ms, cache_hit) "
    "VALUES (%(created_at)s, %(user_id)s, %(event_type)s, %(tool_name)s, "
    " %(prompt_name)s, %(sport)s, %(params)s, %(success)s, %(error_type)s, "
    " %(latency_ms)s, %(cache_hit)s)"
).format(table=config.TABLE)


def _connect():
    import psycopg
    return psycopg.connect(config.DATABASE_URL, autocommit=True, connect_timeout=10)


def _write_batch(conn, batch: "list[dict]") -> None:
    from psycopg.types.json import Jsonb
    rows = []
    for r in batch:
        r = dict(r)
        if r.get("params") is not None:
            r["params"] = Jsonb(r["params"])
        rows.append(r)
    with conn.cursor() as cur:
        cur.executemany(_INSERT_SQL, rows)


def _drain(max_batch: int = 100, timeout: float = 2.0) -> "list[dict]":
    items: "list[dict]" = []
    try:
        items.append(_queue.get(timeout=timeout))
    except queue.Empty:
        return items
    for _ in range(max_batch - 1):
        try:
            items.append(_queue.get_nowait())
        except queue.Empty:
            break
    return items


def _run() -> None:
    conn = None
    backoff = 1.0
    while True:
        batch: "list[dict]" = []
        try:
            batch = _drain()
            if not batch:
                continue
            if conn is None:
                conn = _connect()
            _write_batch(conn, batch)
            backoff = 1.0
        except Exception as exc:
            # Best-effort: log, drop the batch, reconnect next time. A failing DB
            # must never wedge the writer or grow memory without bound.
            logger.warning("usage writer error (%d events dropped): %s", len(batch), exc)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            time.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)


def _flush_on_exit() -> None:
    """Best-effort drain of anything still queued at interpreter shutdown."""
    try:
        if _queue.empty():
            return
        batch: "list[dict]" = []
        while not _queue.empty() and len(batch) < 5000:
            try:
                batch.append(_queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return
        conn = _connect()
        try:
            _write_batch(conn, batch)
        finally:
            conn.close()
    except Exception:
        logger.debug("flush on exit failed", exc_info=True)
