"""
Integration tests for the MCP server layer — tools and prompts.
Tests the structured response shape {success, source, data, suggestions}
and verifies prompts generate with required expert keywords.

Run: .venv/bin/python -m pytest tests/test_server.py -v -s
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from server.main import (
    set_league_profile, get_league_profile, how_to_use,
    get_player_season_stats, get_player_recent, get_player_statcast,
    get_probable_pitchers, get_injuries, get_park_factors, compare_players,
    resolve_player_name,
    weekly_lineup_review, buy_low_finder, sell_high_finder,
    streaming_pitchers, trade_evaluator, waiver_targets,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assert_shape(r: dict, *, source_contains: str = ""):
    """Assert the standard structured response shape."""
    assert r.get("success") is True, f"Tool returned error: {r.get('error')}"
    assert "data" in r, "Missing 'data' key"
    assert "source" in r, "Missing 'source' key"
    assert "suggestions" in r, "Missing 'suggestions' key"
    assert isinstance(r["suggestions"], list), "'suggestions' must be a list"
    if source_contains:
        assert source_contains.lower() in r["source"].lower(), (
            f"Expected source to contain '{source_contains}', got '{r['source']}'"
        )


# ---------------------------------------------------------------------------
# League profile
# ---------------------------------------------------------------------------

def test_set_and_get_profile():
    r = set_league_profile(
        scoring_type="h2h_categories",
        hitting_categories="R,HR,RBI,SB,AVG",
        pitching_categories="W,SV,K,ERA,WHIP",
        lineup_lock="daily",
        league_size=10,
        league_style="redraft",
    )
    assert_shape(r, source_contains="profile")
    assert "saved" in r["data"]["message"].lower()

    r2 = get_league_profile()
    assert_shape(r2)
    assert r2["data"]["profile"]["scoring_type"] == "h2h_categories"
    assert r2["data"]["profile"]["league_size"] == 10
    print(f"\nProfile summary: {r2['data']['summary']}")


def test_set_profile_invalid_scoring_type():
    r = set_league_profile(scoring_type="invalid_type")
    assert r["success"] is False
    assert "error" in r


def test_get_profile_no_profile(tmp_path, monkeypatch):
    """Profile storage returns None for an unseen user."""
    import sources.profile as pm
    orig = pm._storage
    pm._storage = pm.JsonFileStorage(data_dir=tmp_path)
    try:
        r = get_league_profile()
        assert r["success"] is True
        assert r["data"]["profile"] is None
        assert len(r["suggestions"]) > 0  # should nudge user to set profile
    finally:
        pm._storage = orig


# ---------------------------------------------------------------------------
# how_to_use
# ---------------------------------------------------------------------------

def test_how_to_use_general():
    r = how_to_use()
    assert_shape(r, source_contains="StatsDeck")
    assert "weekly_lineup_review" in r["data"]["content"]
    assert "buy_low_finder" in r["data"]["content"]


def test_how_to_use_topic_buy_low():
    r = how_to_use("buy low")
    assert_shape(r)
    assert "xwOBA" in r["data"]["content"]
    assert "barrel" in r["data"]["content"].lower()


def test_how_to_use_fuzzy_topic():
    r = how_to_use("streaming pitchers")
    assert_shape(r)
    assert r["success"]


# ---------------------------------------------------------------------------
# Tool structured responses (network calls)
# ---------------------------------------------------------------------------

def test_season_stats_structured():
    r = get_player_season_stats("Shohei Ohtani", 2024)
    assert_shape(r, source_contains="MLB Stats API")
    assert "stats" in r["data"]
    assert len(r["suggestions"]) > 0
    print(f"\nOhtani 2024 suggestion: {r['suggestions'][0]}")


def test_recent_games_structured():
    r = get_player_recent("Freddie Freeman", 14)
    assert_shape(r, source_contains="MLB Stats API")
    assert "games" in r["data"]
    assert "games_played" in r["data"]
    assert len(r["suggestions"]) > 0
    print(f"\nFreeman recent suggestion: {r['suggestions'][0]}")


def test_statcast_structured():
    r = get_player_statcast("Freddie Freeman", 30)
    assert_shape(r, source_contains="Baseball Savant")
    assert "metrics" in r["data"]
    assert len(r["suggestions"]) > 0
    print(f"\nFreeman Statcast: {r['data']['metrics']}")
    print(f"Suggestions: {r['suggestions']}")


def test_probable_pitchers_structured():
    r = get_probable_pitchers()
    assert_shape(r, source_contains="MLB Stats API")
    assert "games" in r["data"]
    assert len(r["suggestions"]) > 0


def test_park_factors_hitters_park():
    r = get_park_factors("Coors Field")
    assert_shape(r, source_contains="Baseball Savant")
    assert r["data"]["run_factor"] > 100
    assert any("hitter" in s.lower() for s in r["suggestions"])


def test_park_factors_pitchers_park():
    r = get_park_factors("Petco Park")
    assert_shape(r)
    assert r["data"]["run_factor"] < 100
    assert any("pitcher" in s.lower() for s in r["suggestions"])


def test_injuries_team_structured():
    r = get_injuries("dodgers")
    assert_shape(r, source_contains="MLB Stats API")
    assert r["data"]["query_type"] == "team"
    print(f"\nDodgers IL count: {r['data']['count']}")


def test_compare_players_structured():
    r = compare_players("Freddie Freeman", "Paul Goldschmidt", 14)
    assert_shape(r, source_contains="Baseball Savant")
    assert "player_a" in r["data"]
    assert "player_b" in r["data"]
    assert len(r["suggestions"]) > 0
    print(f"\nCompare suggestion: {r['suggestions'][0]}")


def test_resolve_player_name():
    r = resolve_player_name("Shohei Ohtani")
    assert_shape(r)
    assert r["data"]["player"]["mlbam_id"] == 660271


def test_tool_error_shape():
    """Errors should also have consistent shape."""
    r = get_player_recent("Zzzzz Fakeplayer99", 7)
    assert r["success"] is False
    assert "error" in r


# ---------------------------------------------------------------------------
# MCP Prompts — content verification (no network needed)
# ---------------------------------------------------------------------------

ROSTER = "Shohei Ohtani, Freddie Freeman, Aaron Judge, Mookie Betts"
PLAYERS = "Pete Alonso, Christian Yelich, Jorge Polanco"


def test_prompt_weekly_lineup_review():
    p = weekly_lineup_review(roster=ROSTER)
    assert isinstance(p, str) and len(p) > 200
    for keyword in ["get_player_recent", "get_probable_pitchers", "get_player_statcast",
                    "start", "park"]:
        assert keyword in p, f"Missing keyword: {keyword}"


def test_prompt_buy_low_finder():
    p = buy_low_finder(players=PLAYERS)
    assert isinstance(p, str)
    for keyword in ["xwOBA", "barrel", "regression", "Strong Buy"]:
        assert keyword in p, f"Missing keyword: {keyword}"


def test_prompt_sell_high_finder():
    p = sell_high_finder(players=PLAYERS)
    for keyword in ["BABIP", "xwOBA", "Strong Sell"]:
        assert keyword in p, f"Missing keyword: {keyword}"


def test_prompt_streaming_pitchers():
    p = streaming_pitchers(priorities="ratios")
    for keyword in ["get_probable_pitchers", "get_park_factors", "Tier"]:
        assert keyword in p, f"Missing keyword: {keyword}"
    assert "ratios" in p.lower()


def test_prompt_trade_evaluator():
    p = trade_evaluator(giving_up="Pete Alonso", getting="Christian Yelich, Jorge Polanco")
    for keyword in ["get_player_statcast", "get_injuries", "Accept", "xwOBA"]:
        assert keyword in p, f"Missing keyword: {keyword}"


def test_prompt_waiver_targets():
    p = waiver_targets(available_players=PLAYERS, roster_needs="need speed")
    assert "need speed" in p
    for keyword in ["get_player_recent", "Tier 1", "Tier 2"]:
        assert keyword in p, f"Missing keyword: {keyword}"


def test_prompts_embed_league_profile():
    """Prompts should embed league categories from the saved profile."""
    # Profile is set by test_set_and_get_profile; use direct call to ensure it
    set_league_profile(
        scoring_type="roto_categories",
        hitting_categories="R,HR,RBI,SB,AVG",
        pitching_categories="W,SV,K,ERA,WHIP",
    )
    p = buy_low_finder(players=PLAYERS)
    assert "roto" in p.lower() or "R, HR" in p, "Profile not embedded in prompt"


def test_prompt_daily_vs_weekly_framing():
    """streaming_pitchers should adapt framing based on lineup lock."""
    set_league_profile(scoring_type="h2h_categories", lineup_lock="weekly")
    p_weekly = streaming_pitchers()
    assert "weekly" in p_weekly.lower() or "Weekly" in p_weekly

    set_league_profile(scoring_type="h2h_categories", lineup_lock="daily")
    p_daily = streaming_pitchers()
    assert "daily" in p_daily.lower() or "Daily" in p_daily
