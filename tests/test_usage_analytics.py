"""
Tests for the additive usage-analytics layer.

These run WITHOUT a database: a synchronous in-memory sink captures the rows the
instrumentation would have written, so we can assert on them directly. The whole
point of the design is that the tool path never depends on the DB, which is
exactly what makes it unit-testable here.

Run: .venv/bin/python -m pytest tests/test_usage_analytics.py -v
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

import analytics.config as config
import analytics.recorder as recorder
from analytics.instrument import instrument_tool, instrument_prompt
import cache


@pytest.fixture
def captured(monkeypatch):
    """Enable analytics and capture emitted rows synchronously."""
    rows = []
    monkeypatch.setattr(config, "ENABLED", True)
    recorder.set_sink(rows.append)
    yield rows
    recorder.reset_sink()


# ---------------------------------------------------------------------------
# Dummy tools — exercise the decorator without any network/DB.
# ---------------------------------------------------------------------------

@instrument_tool
def _ok_tool(player_name: str, days: int = 14) -> dict:
    return {"success": True, "data": {"player": player_name}, "source": "test"}


@instrument_tool
def _err_tool(player_name: str) -> dict:
    return {"success": False, "error": "days must be between 1 and 90"}


@instrument_tool
def _raising_tool(x: int) -> dict:
    raise ValueError("boom")


@instrument_prompt
def _a_prompt(roster: str) -> str:
    return f"roster: {roster}"


# ---------------------------------------------------------------------------
# Core behavior
# ---------------------------------------------------------------------------

def test_tool_call_records_event(captured):
    out = _ok_tool("Aaron Judge", days=7)
    assert out["success"] is True              # behavior unchanged
    assert len(captured) == 1
    ev = captured[0]
    assert ev["event_type"] == "tool_call"
    assert ev["tool_name"] == "_ok_tool"
    assert ev["sport"] == "mlb"
    assert ev["success"] is True
    assert ev["error_type"] is None
    assert ev["params"] == {"player_name": "Aaron Judge", "days": 7}
    assert isinstance(ev["latency_ms"], int) and ev["latency_ms"] >= 0
    assert "created_at" in ev


def test_params_apply_defaults(captured):
    _ok_tool("Mookie Betts")  # days defaulted
    assert captured[0]["params"] == {"player_name": "Mookie Betts", "days": 14}


def test_error_result_is_classified(captured):
    out = _err_tool("Nobody")
    assert out["success"] is False
    ev = captured[0]
    assert ev["success"] is False
    assert ev["error_type"] == "days_must_be_between_1_and_90"


def test_raising_tool_reraises_but_still_records(captured):
    with pytest.raises(ValueError):
        _raising_tool(1)
    assert len(captured) == 1
    assert captured[0]["success"] is False
    assert captured[0]["error_type"] == "ValueError"


def test_prompt_records_event(captured):
    out = _a_prompt("Judge, Betts")
    assert out == "roster: Judge, Betts"       # behavior unchanged
    ev = captured[0]
    assert ev["event_type"] == "prompt_used"
    assert ev["prompt_name"] == "_a_prompt"
    assert ev["tool_name"] is None


# ---------------------------------------------------------------------------
# Safety guarantees — the whole point of the feature
# ---------------------------------------------------------------------------

def test_disabled_is_a_noop(monkeypatch):
    """No DATABASE_URL → analytics disabled → nothing recorded, tool still works."""
    rows = []
    monkeypatch.setattr(config, "ENABLED", False)
    recorder.set_sink(rows.append)
    try:
        out = _ok_tool("Shohei Ohtani")
    finally:
        recorder.reset_sink()
    assert out["success"] is True
    assert rows == []


def test_sink_failure_never_breaks_tool(monkeypatch):
    """If the recorder itself blows up, the tool must still return normally."""
    monkeypatch.setattr(config, "ENABLED", True)

    def boom(_row):
        raise RuntimeError("sink exploded")

    recorder.set_sink(boom)
    try:
        out = _ok_tool("Freddie Freeman")
    finally:
        recorder.reset_sink()
    assert out["success"] is True   # tool unaffected by analytics failure


def test_oversized_params_are_dropped(captured):
    @instrument_tool
    def _big(payload: str) -> dict:
        return {"success": True}

    _big("x" * 5000)
    ev = captured[0]
    assert ev["params"] == {"_truncated": True, "_keys": ["payload"]}


# ---------------------------------------------------------------------------
# Cache hit/miss detection (via the cache layer's contextvar accounting)
# ---------------------------------------------------------------------------

def test_cache_stats_track_hits_and_misses():
    cache.reset_cache_stats()
    key = cache.make_key("usage-test", os.getpid())
    assert cache.get(key) is None          # miss
    cache.set(key, {"v": 1}, ttl=60)
    assert cache.get(key) == {"v": 1}      # hit
    hits, misses = cache.get_cache_stats()
    assert hits == 1 and misses == 1


def test_cache_hit_reflected_in_event(captured):
    key = cache.make_key("usage-test-hit", os.getpid())
    cache.set(key, {"v": 2}, ttl=60)

    @instrument_tool
    def _cached_tool() -> dict:
        cache.get(key)   # guaranteed hit
        return {"success": True}

    _cached_tool()
    assert captured[0]["cache_hit"] is True


def test_no_lookup_leaves_cache_hit_null(captured):
    _ok_tool("Juan Soto")   # never touches cache
    assert captured[0]["cache_hit"] is None
