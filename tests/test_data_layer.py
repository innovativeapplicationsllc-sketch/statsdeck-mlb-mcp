"""
Smoke tests for the data layer — hit real APIs, check shape not values.
Run: .venv/bin/python -m pytest tests/test_data_layer.py -v -s
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from sources import player_resolver, mlb_stats, savant


# ---------------------------------------------------------------------------
# Player resolver
# ---------------------------------------------------------------------------

def test_resolve_known_player():
    result = player_resolver.resolve_player("Shohei Ohtani")
    assert result is not None
    assert result["player"]["mlbam_id"] is not None
    print(f"\nOhtani resolved: {result['player']}")


def test_resolve_ambiguous():
    result = player_resolver.resolve_player("Rodriguez")
    assert result is not None  # should return best match
    print(f"\nRodriguez best match: {result['player']['name_display']}, alternatives: {[p['name_display'] for p in result['alternatives']]}")


def test_resolve_unknown():
    result = player_resolver.resolve_player("Zzzzz Fakeplayer99")
    assert result is None


# ---------------------------------------------------------------------------
# MLB Stats API
# ---------------------------------------------------------------------------

def test_probable_pitchers_today():
    result = mlb_stats.get_probable_pitchers()
    assert result["source"] == "MLB Stats API"
    assert "games" in result
    print(f"\nProbable pitchers today: {result['game_count']} games")
    for g in result["games"][:3]:
        print(f"  {g['away_team']} ({g['away_pitcher']}) @ {g['home_team']} ({g['home_pitcher']})")


def test_season_stats():
    result = mlb_stats.get_player_season_stats("Shohei Ohtani", 2024)
    assert result["source"] == "MLB Stats API"
    assert "stats" in result
    print(f"\nOhtani 2024 stats keys: {list(result['stats'].keys())}")
    for group, stats in result["stats"].items():
        print(f"  {group}: {list(stats.keys())[:8]}")


def test_recent_games():
    result = mlb_stats.get_player_recent("Shohei Ohtani", days=14)
    assert result["source"] == "MLB Stats API"
    assert "games" in result
    print(f"\nOhtani last 14 days: {result['games_played']} games")


def test_injuries_team():
    result = mlb_stats.get_injuries("dodgers")
    assert result["source"] == "MLB Stats API"
    print(f"\nDodgers IL: {result}")


# ---------------------------------------------------------------------------
# Savant / Statcast
# ---------------------------------------------------------------------------

def test_statcast_batter():
    result = savant.get_player_statcast("Freddie Freeman", days=30)
    assert result["source"].startswith("Baseball Savant")
    print(f"\nFreeman Statcast: {result.get('metrics', result.get('note', 'no data'))}")


def test_park_factors():
    result = savant.get_park_factors("Coors Field")
    assert result["source"].startswith("Baseball Savant")
    assert result.get("run_factor", 0) > 100
    print(f"\nCoors: run={result['run_factor']}, hr={result['hr_factor']} — {result['interpretation']}")


def test_park_factors_team_abbr():
    result = savant.get_park_factors("COL")
    assert result.get("run_factor") is not None
    print(f"\nCOL park: {result['stadium']} run={result['run_factor']}")
