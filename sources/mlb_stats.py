"""
MLB Stats API source — https://statsapi.mlb.com/api/v1/
Free, no auth required. Used for season stats, game logs,
probable pitchers, injuries, and box scores.
"""

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any

import httpx

import cache
from sources.player_resolver import require_player

logger = logging.getLogger(__name__)

BASE_URL = "https://statsapi.mlb.com/api/v1"
TIMEOUT = 15.0

TTL_SEASON = int(os.getenv("CACHE_TTL_SEASON_STATS", 3600))
TTL_RECENT = int(os.getenv("CACHE_TTL_RECENT", 900))
TTL_PITCHERS = int(os.getenv("CACHE_TTL_PITCHERS", 1800))
TTL_INJURIES = int(os.getenv("CACHE_TTL_INJURIES", 600))


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"MLB API error {exc.response.status_code} for {path}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"MLB API network error for {path}: {exc}") from exc


def _search_player(player_name: str) -> int:
    """Resolve name → MLB AM ID (raises ValueError on failure)."""
    pid = require_player(player_name)
    mlbam = pid.get("mlbam_id")
    if not mlbam:
        raise ValueError(f"No MLBAM ID found for '{player_name}' — player may be retired or name misspelled.")
    return mlbam


# ---------------------------------------------------------------------------
# Season stats
# ---------------------------------------------------------------------------

def get_player_season_stats(player_name: str, season: int | None = None) -> dict:
    """
    Return batting and/or pitching season stats from the MLB Stats API.
    season defaults to current year.
    """
    season = season or date.today().year
    ckey = cache.make_key("season_stats", player_name.lower(), season)
    if (hit := cache.get(ckey)) is not None:
        return hit

    mlbam_id = _search_player(player_name)
    player_info = _get(f"/people/{mlbam_id}")
    person = player_info.get("people", [{}])[0]
    pos = person.get("primaryPosition", {}).get("abbreviation", "")

    stat_groups = []
    if pos in ("SP", "RP", "P", "TWP"):
        stat_groups.append("pitching")
    if pos not in ("SP", "RP", "P"):
        stat_groups.append("hitting")
    if not stat_groups:
        stat_groups = ["hitting", "pitching"]

    result: dict[str, Any] = {
        "source": "MLB Stats API",
        "player": player_info.get("people", [{}])[0].get("fullName", player_name),
        "season": season,
        "position": pos,
        "stats": {},
    }

    for group in stat_groups:
        data = _get(f"/people/{mlbam_id}/stats", params={
            "stats": "season",
            "group": group,
            "season": season,
        })
        splits = data.get("stats", [{}])[0].get("splits", []) if data.get("stats") else []
        if splits:
            result["stats"][group] = splits[0].get("stat", {})

    if not result["stats"]:
        result["note"] = f"No {season} stats found — player may not have appeared yet this season."

    cache.set(ckey, result, TTL_SEASON)
    return result


# ---------------------------------------------------------------------------
# Recent game logs
# ---------------------------------------------------------------------------

def get_player_recent(player_name: str, days: int = 14) -> dict:
    """
    Return per-game stats for the last `days` days from the MLB Stats API.
    """
    mlbam_id = _search_player(player_name)
    ckey = cache.make_key("recent_games", player_name.lower(), days, date.today().isoformat())
    if (hit := cache.get(ckey)) is not None:
        return hit

    end = date.today()
    start = end - timedelta(days=days)

    person = _get(f"/people/{mlbam_id}")
    pos = person.get("people", [{}])[0].get("primaryPosition", {}).get("abbreviation", "")
    group = "pitching" if pos in ("SP", "RP", "P") else "hitting"

    data = _get(f"/people/{mlbam_id}/stats", params={
        "stats": "gameLog",
        "group": group,
        "startDate": start.strftime("%m/%d/%Y"),
        "endDate": end.strftime("%m/%d/%Y"),
    })

    splits = data.get("stats", [{}])[0].get("splits", []) if data.get("stats") else []

    games = []
    for s in splits:
        game = s.get("stat", {}).copy()
        game["date"] = s.get("date", "")
        game["opponent"] = s.get("opponent", {}).get("name", "")
        game["home_away"] = "home" if s.get("isHome") else "away"
        games.append(game)

    result = {
        "source": "MLB Stats API",
        "player": person.get("people", [{}])[0].get("fullName", player_name),
        "position": pos,
        "stat_group": group,
        "period_days": days,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "games": games,
        "games_played": len(games),
    }

    cache.set(ckey, result, TTL_RECENT)
    return result


# ---------------------------------------------------------------------------
# Probable pitchers
# ---------------------------------------------------------------------------

def _get_schedule(game_date: str) -> list[dict]:
    """Fetch schedule with linescore for a single date."""
    data = _get("/schedule", params={
        "sportId": 1,
        "date": game_date,
        "hydrate": "probablePitcher(note),team",
        "fields": "dates,games,teams,probablePitcher,fullName,id,team,name",
    })
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games


def get_probable_pitchers(game_date: str | None = None) -> dict:
    """
    Return probable starters for all games on `game_date` (YYYY-MM-DD).
    Defaults to today.
    """
    game_date = game_date or date.today().isoformat()
    ckey = cache.make_key("probable_pitchers", game_date)
    if (hit := cache.get(ckey)) is not None:
        return hit

    games = _get_schedule(game_date)
    matchups = []
    for g in games:
        home = g.get("teams", {}).get("home", {})
        away = g.get("teams", {}).get("away", {})
        home_pp = home.get("probablePitcher", {})
        away_pp = away.get("probablePitcher", {})
        matchups.append({
            "home_team": home.get("team", {}).get("name", ""),
            "away_team": away.get("team", {}).get("name", ""),
            "home_pitcher": home_pp.get("fullName", "TBD"),
            "home_pitcher_id": home_pp.get("id"),
            "away_pitcher": away_pp.get("fullName", "TBD"),
            "away_pitcher_id": away_pp.get("id"),
            "game_pk": g.get("gamePk"),
        })

    result = {
        "source": "MLB Stats API",
        "date": game_date,
        "games": matchups,
        "game_count": len(matchups),
    }
    cache.set(ckey, result, TTL_PITCHERS)
    return result


# ---------------------------------------------------------------------------
# Injuries / IL
# ---------------------------------------------------------------------------

_TEAM_NAME_MAP: dict[str, int] = {}  # populated lazily


def _load_teams() -> dict[str, int]:
    global _TEAM_NAME_MAP
    if _TEAM_NAME_MAP:
        return _TEAM_NAME_MAP
    ckey = "mlb_teams_v1"
    if (hit := cache.get(ckey)) is not None:
        _TEAM_NAME_MAP = hit
        return hit
    data = _get("/teams", params={"sportId": 1})
    mapping = {}
    for t in data.get("teams", []):
        full_name = t.get("name", "").lower()       # "los angeles dodgers"
        abbr = t.get("abbreviation", "").lower()    # "lad"
        short = t.get("teamName", "").lower()       # "dodgers"
        franchise = t.get("franchiseName", "").lower()  # "los angeles"
        team_id = t.get("id")
        if team_id:
            for key in (full_name, abbr, short, franchise):
                if key:
                    mapping[key] = team_id
    cache.set(ckey, mapping, 86400)  # 24 h
    _TEAM_NAME_MAP = mapping
    return mapping


def _fuzzy_team_id(normalized: str, teams: dict[str, int]) -> int | None:
    """Fuzzy match a team query to a team ID."""
    from difflib import get_close_matches
    matches = get_close_matches(normalized, teams.keys(), n=1, cutoff=0.6)
    return teams[matches[0]] if matches else None


def _get_roster_il(team_id: int) -> list[dict]:
    """
    Fetch current IL players for a team using the 40-man roster hydrated with
    injury notes. Players actively on IL have a statusCode of 'DL' or similar
    non-active codes.
    """
    # Use fullRoster with injury hydration for accurate IL status
    data = _get(f"/teams/{team_id}/roster", params={
        "rosterType": "fullRoster",
        "hydrate": "person(injuries)",
    })
    il = []
    for p in data.get("roster", []):
        status_code = p.get("status", {}).get("code", "")
        # Only true IL placements for fantasy-relevant purposes
        IL_CODES = {"D7", "D10", "D15", "D60", "BRV", "SUS", "PAT", "MIN"}
        if status_code in IL_CODES:
            person = p.get("person", {})
            il.append({
                "player": person.get("fullName", ""),
                "player_id": person.get("id"),
                "il_type": p.get("status", {}).get("description", status_code),
                "status_code": status_code,
                "jersey": p.get("jerseyNumber", ""),
            })
    return il


def get_injuries(team_or_player: str | None = None) -> dict:
    """
    Return IL/injury data.
    - If a team name/abbreviation: return that team's IL.
    - If a player name: check if they appear on any IL.
    - If None: return all teams' IL (slow, use sparingly).
    """
    ckey = cache.make_key("injuries", str(team_or_player).lower() if team_or_player else "all")
    if (hit := cache.get(ckey)) is not None:
        return hit

    teams = _load_teams()
    result: dict[str, Any] = {"source": "MLB Stats API"}

    if team_or_player:
        normalized = team_or_player.lower().strip()
        team_id = teams.get(normalized) or _fuzzy_team_id(normalized, teams)

        if team_id:
            il = _get_roster_il(team_id)
            result.update({
                "query_type": "team",
                "team": team_or_player,
                "injured_list": il,
                "count": len(il),
            })
        else:
            # Treat as player name — search their team's IL
            try:
                pid = require_player(team_or_player)
                mlbam_id = pid.get("mlbam_id")
                if mlbam_id:
                    person_data = _get(f"/people/{mlbam_id}")
                    person = person_data.get("people", [{}])[0]
                    current_team_id = person.get("currentTeam", {}).get("id")
                    player_full = person.get("fullName", team_or_player)
                    if current_team_id:
                        il = _get_roster_il(current_team_id)
                        on_il = [p for p in il if p["player_id"] == mlbam_id]
                        result.update({
                            "query_type": "player",
                            "player": player_full,
                            "team": person.get("currentTeam", {}).get("name", ""),
                            "on_injured_list": bool(on_il),
                            "injury_details": on_il[0] if on_il else None,
                        })
                    else:
                        result.update({"query_type": "player", "player": player_full,
                                       "note": "No current team found — player may be a free agent."})
                else:
                    result["error"] = f"Could not find MLBAM ID for '{team_or_player}'"
            except ValueError as exc:
                result["error"] = str(exc)
    else:
        # All teams — expensive; cache longer
        all_il = {}
        for name, tid in teams.items():
            if len(name) > 4:  # skip abbreviations to avoid dupes
                try:
                    all_il[name] = _get_roster_il(tid)
                except Exception:
                    pass
        result.update({"query_type": "all", "teams": all_il})

    cache.set(ckey, result, TTL_INJURIES)
    return result
