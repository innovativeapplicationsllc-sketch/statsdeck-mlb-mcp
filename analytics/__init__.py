"""
StatsDeck usage analytics — additive, invisible, non-blocking per-user usage logging.

Public surface:
    instrument_tool / instrument_prompt  — decorators applied under @mcp.tool / @mcp.prompt
    record_event                         — low-level event emitter (used by the decorators)
    ENABLED                              — True only when DATABASE_URL is configured

Nothing here ever blocks or breaks a tool call: when DATABASE_URL is unset the
whole subsystem is a no-op, and when it's set all DB I/O happens on a background
thread that fails silently (logged server-side) if the database is unreachable.
"""

from .config import ENABLED
from .instrument import instrument_tool, instrument_prompt
from .recorder import record_event

__all__ = ["ENABLED", "instrument_tool", "instrument_prompt", "record_event"]
