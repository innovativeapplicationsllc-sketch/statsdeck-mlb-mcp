"""
Disk-backed cache with per-key TTLs.
Swap the backend here (e.g. Redis) without touching callers.
"""

import os
import functools
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable

import diskcache

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(os.getenv("CACHE_DIR", "") or Path(__file__).parent / "data")
_cache: diskcache.Cache | None = None


def _get_cache() -> diskcache.Cache:
    global _cache
    if _cache is None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache = diskcache.Cache(str(_CACHE_DIR), size_limit=500 * 1024 * 1024)  # 500 MB cap
    return _cache


def get(key: str) -> Any | None:
    try:
        return _get_cache().get(key)
    except Exception as exc:
        logger.warning("Cache read error for %s: %s", key, exc)
        return None


def set(key: str, value: Any, ttl: int) -> None:
    try:
        _get_cache().set(key, value, expire=ttl)
    except Exception as exc:
        logger.warning("Cache write error for %s: %s", key, exc)


def make_key(*parts: Any) -> str:
    """Stable cache key from arbitrary parts."""
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def cached(ttl: int, key_prefix: str = ""):
    """
    Decorator for sync functions. Caches the return value.
    ttl is in seconds. key is derived from prefix + args.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
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


def evict_prefix(prefix: str) -> int:
    """Remove all keys starting with prefix. Returns count removed."""
    cache = _get_cache()
    removed = 0
    for key in list(cache.iterkeys()):
        if isinstance(key, str) and key.startswith(prefix):
            cache.delete(key)
            removed += 1
    return removed
