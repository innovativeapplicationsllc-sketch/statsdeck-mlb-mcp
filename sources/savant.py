"""
Baseball Savant source — Statcast data via pybaseball.
ONLY source for: barrel rate, xwOBA, exit velocity, hard-hit%,
batter-vs-pitcher splits, park factors.

Rate-limit note: Savant throttles heavy scrapers. All results are cached
with long TTLs (3h for Statcast, 24h for park factors) by default.
"""

import logging
import os
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
from pybaseball import (
    playerid_lookup,
    statcast_batter,
    statcast_pitcher,
    pitching_stats_bref,
    batting_stats_bref,
)

import cache
from sources.player_resolver import require_player

logger = logging.getLogger(__name__)

TTL_STATCAST = int(os.getenv("CACHE_TTL_STATCAST", 10800))   # 3 hours
TTL_PARK = int(os.getenv("CACHE_TTL_STATCAST", 86400))       # 24 hours for park factors


# ---------------------------------------------------------------------------
# Statcast helpers
# ---------------------------------------------------------------------------

def _safe_mean(series: pd.Series) -> float | None:
    vals = series.dropna()
    return round(float(vals.mean()), 4) if len(vals) > 0 else None


def _safe_pct(mask: pd.Series, total: int) -> float | None:
    if total == 0:
        return None
    return round(float(mask.sum()) / total, 4)


def _statcast_summary(df: pd.DataFrame) -> dict:
    """Aggregate a raw Statcast DataFrame into fantasy-relevant metrics."""
    if df is None or df.empty:
        return {}

    bip = df[df["type"] == "X"] if "type" in df.columns else df  # balls in play
    total_pa = len(df[df["events"].notna()]) if "events" in df.columns else 0

    # Exit velocity / launch angle — only on BIP
    ev_col = "launch_speed" if "launch_speed" in df.columns else None
    la_col = "launch_angle" if "launch_angle" in df.columns else None

    avg_ev = _safe_mean(bip[ev_col]) if ev_col else None
    avg_la = _safe_mean(bip[la_col]) if la_col else None

    # Barrel: launch_speed >= 98 mph AND launch_angle between 26-30° (simplified MLB def)
    barrel_count = 0
    if ev_col and la_col:
        eligible = bip[(bip[ev_col] >= 98) & (bip[la_col].between(26, 30))]
        barrel_count = len(eligible)

    hard_hit = None
    if ev_col:
        hh = bip[bip[ev_col] >= 95]
        hard_hit = _safe_pct(pd.Series([1] * len(hh)), len(bip)) if len(bip) > 0 else None

    # xwOBA
    xwoba = _safe_mean(df["estimated_woba_using_speedangle"]) if "estimated_woba_using_speedangle" in df.columns else None

    # Sprint speed (if available)
    sprint = _safe_mean(df["sprint_speed"]) if "sprint_speed" in df.columns else None

    metrics: dict[str, Any] = {
        "plate_appearances": total_pa,
        "balls_in_play": len(bip),
        "avg_exit_velocity": avg_ev,
        "avg_launch_angle": avg_la,
        "barrel_count": barrel_count,
        "barrel_rate": round(barrel_count / len(bip), 4) if len(bip) > 0 else None,
        "hard_hit_rate": hard_hit,
        "xwoba": xwoba,
        "sprint_speed": sprint,
    }
    return {k: v for k, v in metrics.items() if v is not None}


# ---------------------------------------------------------------------------
# Public: batter Statcast
# ---------------------------------------------------------------------------

def get_player_statcast(player_name: str, days: int = 14) -> dict:
    """
    Return Statcast metrics (barrel rate, xwOBA, EV, hard-hit%) for a player
    over the last `days` days. Batter-first; falls back to pitcher if no data.
    Source: Baseball Savant via pybaseball.
    """
    end = date.today()
    start = end - timedelta(days=days)
    ckey = cache.make_key("statcast", player_name.lower(), days, end.isoformat())
    if (hit := cache.get(ckey)) is not None:
        return hit

    pid = require_player(player_name)
    mlbam_id = pid.get("mlbam_id")
    if not mlbam_id:
        raise ValueError(f"No MLBAM ID for '{player_name}'")

    display = pid["name_display"]
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    # Try batter first, then pitcher
    df = None
    role = "batter"
    try:
        df = statcast_batter(start_str, end_str, player_id=mlbam_id)
    except Exception as exc:
        logger.warning("statcast_batter failed for %s: %s — trying pitcher", display, exc)

    if df is None or df.empty:
        role = "pitcher"
        try:
            df = statcast_pitcher(start_str, end_str, player_id=mlbam_id)
        except Exception as exc:
            logger.warning("statcast_pitcher failed for %s: %s", display, exc)
            df = None

    if df is None or df.empty:
        result = {
            "source": "Baseball Savant (Statcast)",
            "player": display,
            "period_days": days,
            "note": f"No Statcast data found for {display} in the last {days} days.",
        }
        cache.set(ckey, result, TTL_STATCAST)
        return result

    metrics = _statcast_summary(df)
    result = {
        "source": "Baseball Savant (Statcast)",
        "player": display,
        "role": role,
        "period_days": days,
        "start_date": start_str,
        "end_date": end_str,
        "metrics": metrics,
    }
    if pid.get("mlbam_id"):
        result["savant_url"] = f"https://baseballsavant.mlb.com/savant-player/{mlbam_id}"

    cache.set(ckey, result, TTL_STATCAST)
    return result


# ---------------------------------------------------------------------------
# Public: batter vs pitcher
# ---------------------------------------------------------------------------

def get_batter_vs_pitcher(batter_name: str, pitcher_name: str) -> dict:
    """
    Return head-to-head Statcast data for a specific batter–pitcher matchup.
    Uses the full season's Statcast data for both players and filters to ABs
    where batter faced pitcher.
    """
    ckey = cache.make_key("bvp", batter_name.lower(), pitcher_name.lower(),
                          date.today().year)
    if (hit := cache.get(ckey)) is not None:
        return hit

    batter_pid = require_player(batter_name)
    pitcher_pid = require_player(pitcher_name)

    batter_id = batter_pid.get("mlbam_id")
    pitcher_id = pitcher_pid.get("mlbam_id")

    if not batter_id or not pitcher_id:
        raise ValueError("Could not resolve MLBAM IDs for one or both players.")

    season = date.today().year
    start_str = f"{season}-03-01"
    end_str = date.today().strftime("%Y-%m-%d")

    # Pull pitcher's Statcast data and filter to ABs vs this batter
    df = None
    try:
        df = statcast_pitcher(start_str, end_str, player_id=pitcher_id)
    except Exception as exc:
        logger.warning("statcast_pitcher for BvP failed: %s", exc)

    matchup_df = pd.DataFrame()
    if df is not None and not df.empty and "batter" in df.columns:
        matchup_df = df[df["batter"] == batter_id]

    if matchup_df.empty:
        # Try from batter side
        try:
            df2 = statcast_batter(start_str, end_str, player_id=batter_id)
            if df2 is not None and not df2.empty and "pitcher" in df2.columns:
                matchup_df = df2[df2["pitcher"] == pitcher_id]
        except Exception as exc:
            logger.warning("statcast_batter for BvP failed: %s", exc)

    result: dict[str, Any] = {
        "source": "Baseball Savant (Statcast)",
        "batter": batter_pid["name_display"],
        "pitcher": pitcher_pid["name_display"],
        "season": season,
    }

    if matchup_df.empty:
        result["note"] = f"No Statcast matchup data for {batter_pid['name_display']} vs {pitcher_pid['name_display']} this season."
    else:
        events = matchup_df["events"].dropna() if "events" in matchup_df.columns else pd.Series()
        result["matchup"] = {
            "pitches_seen": len(matchup_df),
            "plate_appearances": int(events.count()),
            "outcomes": events.value_counts().to_dict() if len(events) > 0 else {},
            **_statcast_summary(matchup_df),
        }

    cache.set(ckey, result, TTL_STATCAST)
    return result


# ---------------------------------------------------------------------------
# Public: park factors
# ---------------------------------------------------------------------------

# Static park factor data (2024 approximations from Baseball Savant)
# These are run factors relative to league average (100 = avg).
# TODO: fetch dynamically from savant when pybaseball exposes the endpoint.
_PARK_FACTORS_2024: dict[str, dict] = {
    "coors field":          {"team": "COL", "run_factor": 115, "hr_factor": 113},
    "great american ball park": {"team": "CIN", "run_factor": 110, "hr_factor": 117},
    "yankee stadium":       {"team": "NYY", "run_factor": 106, "hr_factor": 115},
    "fenway park":          {"team": "BOS", "run_factor": 108, "hr_factor": 104},
    "petco park":           {"team": "SD",  "run_factor":  93, "hr_factor":  90},
    "oracle park":          {"team": "SF",  "run_factor":  92, "hr_factor":  85},
    "t-mobile park":        {"team": "SEA", "run_factor":  94, "hr_factor":  92},
    "truist park":          {"team": "ATL", "run_factor":  99, "hr_factor": 100},
    "dodger stadium":       {"team": "LAD", "run_factor":  96, "hr_factor":  96},
    "wrigley field":        {"team": "CHC", "run_factor": 103, "hr_factor": 108},
    "busch stadium":        {"team": "STL", "run_factor":  96, "hr_factor":  92},
    "nationals park":       {"team": "WSH", "run_factor":  98, "hr_factor":  99},
    "pnc park":             {"team": "PIT", "run_factor":  93, "hr_factor":  90},
    "american family field":{"team": "MIL", "run_factor":  99, "hr_factor":  97},
    "progressive field":    {"team": "CLE", "run_factor":  96, "hr_factor":  96},
    "comerica park":        {"team": "DET", "run_factor":  95, "hr_factor":  90},
    "kauffman stadium":     {"team": "KC",  "run_factor":  94, "hr_factor":  93},
    "guaranteed rate field":{"team": "CWS", "run_factor":  96, "hr_factor": 100},
    "target field":         {"team": "MIN", "run_factor":  97, "hr_factor":  96},
    "rogers centre":        {"team": "TOR", "run_factor": 100, "hr_factor": 103},
    "camden yards":         {"team": "BAL", "run_factor": 101, "hr_factor": 103},
    "tropicana field":      {"team": "TB",  "run_factor":  96, "hr_factor":  95},
    "citizens bank park":   {"team": "PHI", "run_factor": 104, "hr_factor": 109},
    "citi field":           {"team": "NYM", "run_factor":  97, "hr_factor":  96},
    "minute maid park":     {"team": "HOU", "run_factor":  99, "hr_factor": 100},
    "globe life field":     {"team": "TEX", "run_factor": 102, "hr_factor": 104},
    "angel stadium":        {"team": "LAA", "run_factor":  96, "hr_factor":  95},
    "oakland coliseum":     {"team": "OAK", "run_factor":  92, "hr_factor":  88},
    "chase field":          {"team": "ARI", "run_factor": 105, "hr_factor": 107},
    "suntrust park":        {"team": "ATL", "run_factor":  99, "hr_factor": 100},  # legacy alias
}

# Team abbreviation → stadium lookup
_TEAM_TO_PARK: dict[str, str] = {v["team"].lower(): k for k, v in _PARK_FACTORS_2024.items()}


def get_park_factors(stadium_or_team: str) -> dict:
    """
    Return park factor data for a stadium name or team abbreviation.
    Source: Baseball Savant (static 2024 approximations; live endpoint TBD).
    """
    query = stadium_or_team.lower().strip()
    ckey = cache.make_key("park_factors", query)
    if (hit := cache.get(ckey)) is not None:
        return hit

    # Direct match
    factors = _PARK_FACTORS_2024.get(query)
    if not factors:
        # Try team abbreviation
        park_name = _TEAM_TO_PARK.get(query)
        if park_name:
            factors = _PARK_FACTORS_2024.get(park_name)
            query = park_name

    if not factors:
        # Fuzzy search
        from difflib import get_close_matches
        candidates = list(_PARK_FACTORS_2024.keys()) + list(_TEAM_TO_PARK.keys())
        matches = get_close_matches(stadium_or_team.lower(), candidates, n=3, cutoff=0.5)
        if matches:
            best = matches[0]
            park_name = _TEAM_TO_PARK.get(best, best)
            factors = _PARK_FACTORS_2024.get(park_name)
            query = park_name
        else:
            result = {
                "source": "Baseball Savant (Statcast)",
                "error": f"Stadium or team '{stadium_or_team}' not found.",
                "hint": "Try team abbreviation (e.g. 'COL') or stadium name (e.g. 'Coors Field').",
            }
            cache.set(ckey, result, TTL_PARK)
            return result

    interpretation = _interpret_park(factors.get("run_factor", 100))
    result = {
        "source": "Baseball Savant (Statcast)",
        "stadium": query.title(),
        "team": factors.get("team", ""),
        "season": 2024,
        "run_factor": factors.get("run_factor"),
        "hr_factor": factors.get("hr_factor"),
        "interpretation": interpretation,
        "note": "Factors are relative to league average (100). Data is 2024 approximation; live Savant endpoint planned for v2.",
    }

    cache.set(ckey, result, TTL_PARK)
    return result


def _interpret_park(run_factor: int) -> str:
    if run_factor >= 110:
        return "Extreme hitter's park — significantly boosts all offensive stats."
    if run_factor >= 105:
        return "Hitter's park — moderate boost to offensive production."
    if run_factor >= 98:
        return "Neutral park — near league average."
    if run_factor >= 93:
        return "Pitcher's park — slight suppression of offense."
    return "Strong pitcher's park — significantly suppresses offensive stats."
