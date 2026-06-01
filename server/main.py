"""
MLB Fantasy MCP Server — FastMCP entry point.

Transport is selected by environment variable:
  MCP_TRANSPORT=stdio  (default) — for local Claude Desktop
  MCP_TRANSPORT=http   — for remote deploy (Streamable HTTP)
"""

import logging
import os
import sys
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

# Ensure project root is on path when run as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources import mlb_stats, savant
from sources.player_resolver import resolve_player

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "MLB Fantasy Assistant",
    instructions=(
        "You have access to live MLB data for fantasy baseball analysis. "
        "All stats are from official sources with clear attribution. "
        "Statcast data (barrel rate, xwOBA, exit velocity) comes exclusively "
        "from Baseball Savant. Injury/IL and game-log data comes from the MLB Stats API."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_error(msg: str) -> dict:
    return {"error": msg, "success": False}


def _wrap(fn, *args, **kwargs) -> dict:
    """Call a data-layer function and convert exceptions to clean error dicts."""
    try:
        result = fn(*args, **kwargs)
        return {"success": True, **result}
    except ValueError as exc:
        return _tool_error(str(exc))
    except RuntimeError as exc:
        return _tool_error(f"Data fetch failed: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in tool call")
        return _tool_error(f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Tool: get_player_season_stats
# ---------------------------------------------------------------------------

@mcp.tool()
def get_player_season_stats(player_name: str, season: int | None = None) -> dict:
    """
    Get a player's full season batting or pitching statistics.

    Args:
        player_name: Player's full name (e.g. "Shohei Ohtani", "Freddie Freeman")
        season: MLB season year (defaults to current season)

    Returns:
        Season stats dict with source attribution. Includes standard counting
        stats and rate stats (AVG, OBP, SLG for batters; ERA, WHIP, K/9 for pitchers).
        Source: MLB Stats API.
    """
    return _wrap(mlb_stats.get_player_season_stats, player_name, season)


# ---------------------------------------------------------------------------
# Tool: get_player_recent
# ---------------------------------------------------------------------------

@mcp.tool()
def get_player_recent(player_name: str, days: int = 14) -> dict:
    """
    Get a player's game-by-game stats over the last N days.

    Args:
        player_name: Player's full name
        days: Number of days to look back (default 14, max recommended 30)

    Returns:
        List of game log entries with date, opponent, and per-game stats.
        Useful for identifying hot/cold streaks. Source: MLB Stats API.
    """
    if days < 1 or days > 90:
        return _tool_error("days must be between 1 and 90")
    return _wrap(mlb_stats.get_player_recent, player_name, days)


# ---------------------------------------------------------------------------
# Tool: get_player_statcast
# ---------------------------------------------------------------------------

@mcp.tool()
def get_player_statcast(player_name: str, days: int = 14) -> dict:
    """
    Get Statcast metrics for a player over the last N days.

    Metrics include: exit velocity, launch angle, barrel rate, hard-hit rate,
    and xwOBA (expected weighted on-base average). These are the advanced
    metrics used to evaluate true talent beyond batting average.

    Args:
        player_name: Player's full name
        days: Number of days to look back (default 14)

    Returns:
        Statcast metrics dict. Source: Baseball Savant (ONLY source for Statcast).
        Note: cached for 3 hours to respect Savant rate limits.
    """
    if days < 1 or days > 90:
        return _tool_error("days must be between 1 and 90")
    return _wrap(savant.get_player_statcast, player_name, days)


# ---------------------------------------------------------------------------
# Tool: get_probable_pitchers
# ---------------------------------------------------------------------------

@mcp.tool()
def get_probable_pitchers(game_date: str | None = None) -> dict:
    """
    Get probable starting pitchers for all games on a given date.

    Args:
        game_date: Date in YYYY-MM-DD format (defaults to today)

    Returns:
        List of matchups with home/away team names and probable starters.
        "TBD" indicates pitcher not yet announced. Source: MLB Stats API.
    """
    if game_date:
        try:
            date.fromisoformat(game_date)
        except ValueError:
            return _tool_error(f"Invalid date format '{game_date}'. Use YYYY-MM-DD.")
    return _wrap(mlb_stats.get_probable_pitchers, game_date)


# ---------------------------------------------------------------------------
# Tool: get_batter_vs_pitcher
# ---------------------------------------------------------------------------

@mcp.tool()
def get_batter_vs_pitcher(batter: str, pitcher: str) -> dict:
    """
    Get head-to-head Statcast matchup data between a specific batter and pitcher.

    Useful for start/sit decisions when you know who your players are facing.
    Returns pitch outcomes, exit velocity, xwOBA for this season's matchups.

    Args:
        batter: Batter's full name (e.g. "Freddie Freeman")
        pitcher: Pitcher's full name (e.g. "Zack Wheeler")

    Returns:
        Matchup stats for this season. May show limited data if they haven't
        faced each other much yet. Source: Baseball Savant (Statcast).
    """
    return _wrap(savant.get_batter_vs_pitcher, batter, pitcher)


# ---------------------------------------------------------------------------
# Tool: get_injuries
# ---------------------------------------------------------------------------

@mcp.tool()
def get_injuries(team_or_player: str | None = None) -> dict:
    """
    Get current injured list (IL) status for a team or player.

    Args:
        team_or_player: Team name/abbreviation (e.g. "Dodgers", "LAD") OR
                        player name (e.g. "Mookie Betts") OR
                        None to get all teams' IL (slow)

    Returns:
        For a team: full IL roster with player names and status.
        For a player: whether they are currently on IL and details if so.
        Source: MLB Stats API.
    """
    return _wrap(mlb_stats.get_injuries, team_or_player)


# ---------------------------------------------------------------------------
# Tool: get_park_factors
# ---------------------------------------------------------------------------

@mcp.tool()
def get_park_factors(stadium_or_team: str) -> dict:
    """
    Get park factor data for a stadium or team — how much the ballpark
    inflates or suppresses offense relative to league average (100 = neutral).

    Useful for: start/sit decisions for hitters/pitchers playing in extreme parks,
    understanding if a player's stats are context-inflated.

    Args:
        stadium_or_team: Stadium name (e.g. "Coors Field", "Fenway Park") or
                         team abbreviation (e.g. "COL", "BOS")

    Returns:
        Run factor, HR factor, and plain-English interpretation.
        Source: Baseball Savant (Statcast). 2024 data; live endpoint planned for v2.
    """
    return _wrap(savant.get_park_factors, stadium_or_team)


# ---------------------------------------------------------------------------
# Tool: compare_players
# ---------------------------------------------------------------------------

@mcp.tool()
def compare_players(player_a: str, player_b: str, days: int = 14) -> dict:
    """
    Compare two players side-by-side using both MLB Stats API and Statcast data.

    Combines recent game-log stats (counting stats, plate appearances) with
    Statcast quality-of-contact metrics (xwOBA, barrel rate, exit velocity).
    Ideal for start/sit decisions and waiver wire comparisons.

    Args:
        player_a: First player's full name
        player_b: Second player's full name
        days: Lookback window in days (default 14)

    Returns:
        Side-by-side comparison dict with both players' recent stats and
        Statcast metrics. Sources: MLB Stats API + Baseball Savant.
    """
    if days < 1 or days > 90:
        return _tool_error("days must be between 1 and 90")

    result_a = _wrap(mlb_stats.get_player_recent, player_a, days)
    result_b = _wrap(mlb_stats.get_player_recent, player_b, days)
    statcast_a = _wrap(savant.get_player_statcast, player_a, days)
    statcast_b = _wrap(savant.get_player_statcast, player_b, days)

    def _summary(recent: dict, statcast: dict) -> dict:
        out: dict[str, Any] = {}
        if recent.get("success"):
            out["games_played"] = recent.get("games_played", 0)
            out["recent_games"] = recent.get("games", [])
            out["stat_group"] = recent.get("stat_group", "")
        else:
            out["recent_error"] = recent.get("error", "unknown error")

        if statcast.get("success"):
            out["statcast"] = statcast.get("metrics", {})
            out["statcast_note"] = statcast.get("note", "")
        else:
            out["statcast_error"] = statcast.get("error", "unknown error")
        return out

    return {
        "success": True,
        "source": "MLB Stats API + Baseball Savant (Statcast)",
        "period_days": days,
        "player_a": {
            "name": player_a,
            **_summary(result_a, statcast_a),
        },
        "player_b": {
            "name": player_b,
            **_summary(result_b, statcast_b),
        },
    }


# ---------------------------------------------------------------------------
# Tool: resolve_player_name  (utility / debug)
# ---------------------------------------------------------------------------

@mcp.tool()
def resolve_player_name(player_name: str) -> dict:
    """
    Resolve a player name to their MLB and FanGraphs IDs.

    Useful when you're not sure of the exact spelling, or to disambiguate
    players with the same last name. Returns the best match plus alternatives.

    Args:
        player_name: Any reasonable name variant ("Ohtani", "Shohei Ohtani", etc.)

    Returns:
        Best match player IDs plus up to 4 alternatives if name is ambiguous.
    """
    result = resolve_player(player_name)
    if result is None:
        return _tool_error(f"No player found matching '{player_name}'.")
    return {"success": True, **result}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run():
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8000"))
        logger.info("Starting MCP server on http://%s:%s", host, port)
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        logger.info("Starting MCP server over stdio")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    run()
