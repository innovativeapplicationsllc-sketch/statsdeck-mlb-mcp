"""
Cache layer with three backends selected by environment at startup:

  REDIS_URL set          → Redis (production, multi-instance safe)
  CACHE_DIR set          → diskcache on disk (local dev with persistence)
  neither                → in-memory TTL dict (default; fine for single-instance Railway deploys)

Swapping backends: set/unset the env vars — no code changes.
The public API (get / set / make_key / cached / evict_prefix) is identical
across all three.
"""

import hashlib
import json
import logging
import os
import time
from functools import wraps
from threading import RLock
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------

class _MemoryBackend:
    """
    Thread-safe in-memory TTL cache.  Zero external deps.
    Good default for stateless single-instance deploys (e.g. Railway without Redis).
    Cache is lost on process restart — acceptable for short-TTL data.
    """

    _EVICT_AT = 4000  # trigger passive eviction when store exceeds this

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = RLock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires = entry
            if time.monotonic() > expires:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            if len(self._store) >= self._EVICT_AT:
                self._evict_expired()
            self._store[key] = (value, time.monotonic() + ttl)

    def evict_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._store if k.startswith(prefix)]
            for k in keys:
                del self._store[k]
            return len(keys)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._store.items() if exp < now]
        for k in expired:
            del self._store[k]


class _RedisBackend:
    """
    Redis-backed cache.  Survives restarts, safe for multi-instance.
    Requires: REDIS_URL env var + `redis` package.
    """

    def __init__(self, url: str) -> None:
        import redis as _r
        self._client = _r.from_url(url, decode_responses=False, socket_timeout=2)

    def get(self, key: str) -> Any | None:
        try:
            raw = self._client.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception as exc:
            logger.warning("Redis get error %s: %s", key[:16], exc)
            return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        try:
            self._client.setex(key, ttl, json.dumps(value, default=str))
        except Exception as exc:
            logger.warning("Redis set error %s: %s", key[:16], exc)

    def evict_prefix(self, prefix: str) -> int:
        removed = 0
        cursor = 0
        try:
            while True:
                cursor, keys = self._client.scan(cursor, match=f"{prefix}*", count=100)
                if keys:
                    self._client.delete(*keys)
                    removed += len(keys)
                if cursor == 0:
                    break
        except Exception as exc:
            logger.warning("Redis evict_prefix error: %s", exc)
        return removed


class _DiskBackend:
    """
    diskcache on-disk backend — used when CACHE_DIR is explicitly set.
    Survives restarts; not safe for multi-instance without a shared filesystem.
    """

    def __init__(self, cache_dir: str) -> None:
        import diskcache
        from pathlib import Path
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        self._cache = diskcache.Cache(cache_dir, size_limit=500 * 1024 * 1024)

    def get(self, key: str) -> Any | None:
        try:
            return self._cache.get(key)
        except Exception as exc:
            logger.warning("Disk cache get error %s: %s", key[:16], exc)
            return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        try:
            self._cache.set(key, value, expire=ttl)
        except Exception as exc:
            logger.warning("Disk cache set error %s: %s", key[:16], exc)

    def evict_prefix(self, prefix: str) -> int:
        removed = 0
        try:
            for key in list(self._cache.iterkeys()):
                if isinstance(key, str) and key.startswith(prefix):
                    self._cache.delete(key)
                    removed += 1
        except Exception as exc:
            logger.warning("Disk cache evict_prefix error: %s", exc)
        return removed


# ---------------------------------------------------------------------------
# Backend selection — runs once at import
# ---------------------------------------------------------------------------

def _build_backend() -> _MemoryBackend | _RedisBackend | _DiskBackend:
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        try:
            b = _RedisBackend(redis_url)
            b._client.ping()
            logger.info("Cache backend: Redis")
            return b
        except Exception as exc:
            logger.warning("REDIS_URL set but connection failed (%s) — falling back to memory", exc)

    cache_dir = os.getenv("CACHE_DIR", "").strip()
    if cache_dir:
        logger.info("Cache backend: disk at %s", cache_dir)
        return _DiskBackend(cache_dir)

    logger.info("Cache backend: in-memory (stateless mode)")
    return _MemoryBackend()


_backend = _build_backend()


# ---------------------------------------------------------------------------
# Public API — same interface regardless of backend
# ---------------------------------------------------------------------------

def get(key: str) -> Any | None:
    return _backend.get(key)


def set(key: str, value: Any, ttl: int) -> None:
    _backend.set(key, value, ttl)


def make_key(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def evict_prefix(prefix: str) -> int:
    return _backend.evict_prefix(prefix)


def cached(ttl: int, key_prefix: str = ""):
    """Decorator for sync functions. Caches the return value with the given TTL."""
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = key_prefix + make_key(fn.__name__, args, kwargs)
            hit = get(key)
            if hit is not None:
                logger.debug("Cache HIT %s", key[:16])
                return hit
            result = fn(*args, **kwargs)
            if result is not None:
                set(key, result, ttl)
            return result
        return wrapper
    return decorator
