"""
Player name → {mlbam_id, fangraphs_id, name} resolver.

Uses pybaseball's playerid_lookup (wraps Baseball Reference/Chadwick Bureau).
Results are cached aggressively (1 week) since IDs are stable.

Returns the best single match plus alternatives when the name is ambiguous.
"""

import logging
import os
import re
from difflib import SequenceMatcher
from typing import TypedDict

import pandas as pd
from pybaseball import playerid_lookup

import cache

logger = logging.getLogger(__name__)

CACHE_TTL = int(os.getenv("CACHE_TTL_PLAYER_ID", 604800))  # 1 week default


class PlayerID(TypedDict):
    name: str           # "Last, First" canonical form
    name_display: str   # "First Last"
    mlbam_id: int | None
    fangraphs_id: str | None
    birthyear: int | None


class ResolveResult(TypedDict):
    player: PlayerID
    alternatives: list[PlayerID]  # other matches when name is ambiguous
    ambiguous: bool


def _to_player_id(row: pd.Series) -> PlayerID:
    mlbam = row.get("key_mlbam")
    fg = row.get("key_fangraphs")
    by = row.get("mlb_played_first")
    first = str(row.get("name_first", "")).strip().title()
    last = str(row.get("name_last", "")).strip().title()
    return PlayerID(
        name=f"{last}, {first}",
        name_display=f"{first} {last}",
        mlbam_id=int(mlbam) if pd.notna(mlbam) else None,
        fangraphs_id=str(int(fg)) if pd.notna(fg) else None,
        birthyear=int(by) if pd.notna(by) else None,
    )


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _lookup_raw(last: str, first: str) -> pd.DataFrame:
    """Call pybaseball with caching."""
    key = cache.make_key("playerid_lookup", last.lower(), first.lower())
    hit = cache.get(key)
    if hit is not None:
        return pd.DataFrame(hit)
    try:
        df = playerid_lookup(last, first, fuzzy=True)
    except Exception as exc:
        logger.error("playerid_lookup failed for %s %s: %s", first, last, exc)
        return pd.DataFrame()
    if df is not None and not df.empty:
        cache.set(key, df.to_dict("records"), CACHE_TTL)
    return df if df is not None else pd.DataFrame()


def _parse_name(player_name: str) -> tuple[str, str]:
    """Split 'First Last' or 'Last, First' into (last, first)."""
    player_name = player_name.strip()
    if "," in player_name:
        parts = [p.strip() for p in player_name.split(",", 1)]
        return parts[0], parts[1]
    parts = player_name.split()
    if len(parts) == 1:
        return parts[0], ""
    # Handle name suffixes: Jr., Sr., II, III, IV
    suffix_pattern = re.compile(r"^(jr\.?|sr\.?|ii|iii|iv)$", re.IGNORECASE)
    if len(parts) >= 3 and suffix_pattern.match(parts[-1]):
        return " ".join(parts[1:-1]) + " " + parts[-1], parts[0]
    return " ".join(parts[1:]), parts[0]


def resolve_player(player_name: str) -> ResolveResult | None:
    """
    Resolve a player name to IDs. Returns None if no match found.
    If multiple matches exist, returns the best one plus alternatives.
    """
    cache_key = cache.make_key("resolve_player", player_name.lower().strip())
    hit = cache.get(cache_key)
    if hit is not None:
        return hit

    last, first = _parse_name(player_name)
    df = _lookup_raw(last, first)

    if df is None or df.empty:
        # Try fuzzy: last name only
        df = _lookup_raw(last, "")

    if df is None or df.empty:
        return None

    # Score each row combining full-string AND per-component similarity.
    # Per-component scoring prevents character-overlap false positives
    # (e.g. "Fakeplayer99" ↔ "kepler" has high char overlap but low word match).
    input_last = last.lower()
    input_first = first.lower()
    full_input = player_name.strip().lower()

    def score(row: pd.Series) -> float:
        fn = str(row.get("name_first", "")).lower()
        ln = str(row.get("name_last", "")).lower()
        last_score = _similarity(input_last, ln)
        first_score = _similarity(input_first, fn) if input_first else 0.5
        # Require each component to individually pass a floor.
        # This stops character-overlap flukes (e.g. "fakeplayer99" ↔ "kepler")
        # from surviving when the other component is clearly wrong.
        if input_first and first_score < 0.25 and last_score < 0.75:
            return 0.0
        if last_score < 0.35:
            return 0.0
        full_score = max(
            _similarity(full_input, f"{fn} {ln}"),
            _similarity(full_input, f"{ln} {fn}"),
        )
        return (full_score + last_score * 0.6 + first_score * 0.4) / 2.0

    df = df.copy()
    df["_score"] = df.apply(score, axis=1)
    df = df.sort_values("_score", ascending=False).reset_index(drop=True)

    MIN_SCORE = 0.40
    if float(df.iloc[0]["_score"]) < MIN_SCORE:
        return None

    players = [_to_player_id(df.iloc[i]) for i in range(min(len(df), 5))
               if float(df.iloc[i]["_score"]) >= MIN_SCORE]
    if not players:
        return None

    result = ResolveResult(
        player=players[0],
        alternatives=players[1:],
        ambiguous=len(players) > 1 and df.iloc[0]["_score"] < 0.95,
    )
    cache.set(cache_key, result, CACHE_TTL)
    return result


def require_player(player_name: str) -> PlayerID:
    """
    Like resolve_player but raises ValueError with a helpful message on failure.
    Use this inside tool implementations.
    """
    result = resolve_player(player_name)
    if result is None:
        raise ValueError(f"No player found matching '{player_name}'. Check spelling or try last name only.")
    if result["ambiguous"]:
        alts = ", ".join(p["name_display"] for p in result["alternatives"][:3])
        logger.info("Ambiguous name '%s' → using %s (alternatives: %s)",
                    player_name, result["player"]["name_display"], alts)
    return result["player"]
