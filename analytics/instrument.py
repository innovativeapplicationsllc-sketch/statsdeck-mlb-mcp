"""
Tool / prompt instrumentation decorators.

These wrap a tool (or prompt) function and, AFTER it returns, fire one usage
event off to the background recorder. They are designed to be applied *under*
the FastMCP registration decorator:

    @mcp.tool(annotations=...)
    @instrument_tool
    def get_player_stats(...):
        ...

functools.wraps preserves __name__, __doc__, __wrapped__ and __annotations__, so
FastMCP's signature introspection (inspect.signature follows __wrapped__) sees the
original signature and builds the exact same input schema as before. The wrapper
returns the original result unchanged — tool behavior is untouched.

Only actual tool/prompt invocations are instrumented. MCP discovery requests
(ListTools / ListPrompts) never call these functions, so protocol chatter never
shows up as user activity.
"""

import functools
import inspect
import logging
import re
import time

from .recorder import record_event

logger = logging.getLogger("analytics.instrument")

# This is the MLB server; every event it emits is MLB. When NFL ships as its own
# server, it sets its own sport. Nullable in the schema for that future.
SPORT = "mlb"

# Cache hit/miss accounting is recorded by the cache layer into a contextvar.
# Import is guarded so analytics never hard-depends on the cache module.
try:
    from cache import reset_cache_stats, get_cache_stats
except Exception:  # pragma: no cover
    def reset_cache_stats() -> None:  # type: ignore
        pass

    def get_cache_stats():  # type: ignore
        return (0, 0)


def _safe_user_id() -> str:
    """Clerk subject from the validated OAuth token, or 'default' (stdio / unauth)."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        token = get_access_token()
        if token and token.subject:
            return token.subject
    except Exception:
        pass
    return "default"


def _capture_params(fn, args, kwargs) -> dict:
    """
    Bind call args to the original signature and keep only small scalar params.
    These are non-sensitive query inputs (player names, dates, timeframes,
    categories) — exactly what's useful for analytics, nothing more.
    """
    try:
        sig = inspect.signature(fn)
        bound = sig.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        out = {}
        for key, val in bound.arguments.items():
            if val is None or isinstance(val, (str, int, float, bool)):
                out[key] = val
            else:
                out[key] = str(val)[:200]
        return out
    except Exception:
        return {}


def _classify_error(msg) -> str:
    """Turn a tool error message into a short, non-sensitive error_type slug."""
    if not isinstance(msg, str) or not msg:
        return "error"
    head = msg.split(":", 1)[0].strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", head).strip("_")
    return slug[:64] or "error"


def instrument_tool(fn):
    """Wrap a tool: record one 'tool_call' event after it runs. Never alters behavior."""
    tool_name = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        reset_cache_stats()
        success = None
        error_type = None
        result = None
        try:
            result = fn(*args, **kwargs)
            return result
        except Exception as exc:  # tools normally return _err rather than raise
            success = False
            error_type = type(exc).__name__
            raise
        finally:
            try:
                latency_ms = int((time.perf_counter() - start) * 1000)
                if success is None:
                    if isinstance(result, dict):
                        success = bool(result.get("success", True))
                        if not success:
                            error_type = _classify_error(result.get("error"))
                    else:
                        success = True
                hits, misses = get_cache_stats()
                cache_hit = (misses == 0) if (hits or misses) else None
                record_event(
                    event_type="tool_call",
                    user_id=_safe_user_id(),
                    tool_name=tool_name,
                    sport=SPORT,
                    params=_capture_params(fn, args, kwargs),
                    success=success,
                    error_type=error_type,
                    latency_ms=latency_ms,
                    cache_hit=cache_hit,
                )
            except Exception:
                logger.debug("instrument_tool failed for %s", tool_name, exc_info=True)

    return wrapper


def instrument_prompt(fn):
    """Wrap a prompt: record one 'prompt_used' event after it renders."""
    prompt_name = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        success = True
        error_type = None
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            success = False
            error_type = type(exc).__name__
            raise
        finally:
            try:
                latency_ms = int((time.perf_counter() - start) * 1000)
                record_event(
                    event_type="prompt_used",
                    user_id=_safe_user_id(),
                    prompt_name=prompt_name,
                    sport=SPORT,
                    params=_capture_params(fn, args, kwargs),
                    success=success,
                    error_type=error_type,
                    latency_ms=latency_ms,
                )
            except Exception:
                logger.debug("instrument_prompt failed for %s", prompt_name, exc_info=True)

    return wrapper
