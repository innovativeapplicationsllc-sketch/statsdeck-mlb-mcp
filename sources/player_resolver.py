"""
Player name → {mlbam_id, fangraphs_id, name} resolver.

Uses pybaseball's playerid_lookup (wraps Baseball Reference/Chadwick Bureau).
Results are cached aggressively (1 week) since IDs are stable.

Returns the best single match plus alternatives when the name is ambiguous.
"""

import logging
import os
import re
import unicodedata
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


def _safe_int(v) -> int | None:
    """Coerce to int, tolerating NaN, None, empty strings, and floats like '2019.0'."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _to_player_id(row: pd.Series) -> PlayerID:
    first = str(row.get("name_first", "")).strip().title()
    last = str(row.get("name_last", "")).strip().title()
    fg = _safe_int(row.get("key_fangraphs"))
    return PlayerID(
        name=f"{last}, {first}",
        name_display=f"{first} {last}",
        mlbam_id=_safe_int(row.get("key_mlbam")),
        fangraphs_id=str(fg) if fg is not None else None,
        birthyear=_safe_int(row.get("mlb_played_first")),
    )


def _strip_accents(s: str) -> str:
    """Fold diacritics: 'Acuña' → 'Acuna', 'José' → 'Jose'."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c)
    )


_SUFFIX_RE = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?", re.IGNORECASE)


def _norm_last(s: str) -> str:
    """Normalize a last name for matching: accent-fold, drop suffixes/punctuation."""
    s = _strip_accents(s).lower()
    s = _SUFFIX_RE.sub("", s)
    return re.sub(r"[^a-z]", "", s)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _strip_accents(a).lower(), _strip_accents(b).lower()).ratio()


# ---------------------------------------------------------------------------
# First-name nickname handling.
#
# Player registers store the name a player goes by, which is often a nickname
# ("Mike King", not "Michael King"; "Matt", "Alex", "Nate"...). Plain string
# similarity is NOT enough — e.g. for input "Michael", similarity("michael","mike")
# is 0.55, which is actually LOWER than similarity("michael","hal")=0.60 or
# "charles"=0.57. Without an explicit nickname map the resolver would prefer the
# wrong same-last-name player. Each tuple is a set of interchangeable first names.
# ---------------------------------------------------------------------------

_NICKNAME_GROUPS: list[frozenset[str]] = [
    frozenset(g) for g in (
        {"michael", "mike", "mikey", "micah"},
        {"matthew", "matt"},
        {"alexander", "alex", "alejandro"},
        {"nicholas", "nick", "nicky"},
        {"nathaniel", "nathan", "nate"},
        {"joseph", "joe", "joey"},
        {"jacob", "jake"},
        {"william", "will", "bill", "billy", "willy"},
        {"robert", "rob", "bob", "bobby", "robbie"},
        {"thomas", "tom", "tommy"},
        {"jonathan", "jon", "johnny"},
        {"john", "johnny", "jack"},
        {"james", "jim", "jimmy", "jamie"},
        {"daniel", "dan", "danny"},
        {"christopher", "chris"},
        {"anthony", "tony"},
        {"zachary", "zach", "zac", "zack"},
        {"benjamin", "ben", "benny"},
        {"samuel", "sam", "sammy"},
        {"david", "dave", "davey"},
        {"steven", "stephen", "steve"},
        {"richard", "rich", "rick", "ricky", "dick"},
        {"andrew", "andy", "drew"},
        {"charles", "charlie", "chuck"},
        {"edward", "ed", "eddie", "ted"},
        {"frederick", "fred", "freddie", "freddy"},
        {"kenneth", "ken", "kenny"},
        {"ronald", "ron", "ronnie"},
        {"vincent", "vince", "vinny"},
        {"gabriel", "gabe"},
        {"emmanuel", "manny"},
        {"gerald", "gerry", "jerry"},
        {"raymond", "ray"},
        {"patrick", "pat"},
        {"timothy", "tim", "timmy"},
        {"gregory", "greg"},
        {"joshua", "josh"},
        {"luis", "lou", "louie"},
    )
]

# Reverse index: first name → its nickname group (for O(1) membership checks).
_NICK_INDEX: dict[str, frozenset[str]] = {}
for _grp in _NICKNAME_GROUPS:
    for _n in _grp:
        _NICK_INDEX[_n] = _grp


def _are_nicknames(a: str, b: str) -> bool:
    """True if a and b are known interchangeable first-name forms."""
    grp = _NICK_INDEX.get(a)
    return grp is not None and b in grp


def _first_name_score(input_first: str, candidate_first: str) -> float:
    """
    Score how well an input first name matches a candidate's first name, with
    nickname awareness. Returns 0.5 when the user gave no first name (neutral).
    """
    a = _strip_accents(input_first or "").lower().strip()
    b = _strip_accents(candidate_first or "").lower().strip()
    if b in ("", "nan"):
        b = ""
    if not a:
        return 0.5
    if not b:
        return 0.3
    if a == b or _are_nicknames(a, b):
        return 1.0
    # An initialism ("J" for "Jose") or a clean prefix is a strong signal.
    if (len(a) <= 2 and b.startswith(a)) or b.startswith(a) or a.startswith(b):
        return max(0.85, _similarity(a, b))
    return _similarity(a, b)


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


# Confidence thresholds.
_LAST_GATE = 0.85   # when a last name is supplied, a candidate's last name must
                    # match this well (accent-folded, suffix-stripped) to be
                    # trusted at all (anchors the search)
_MIN_SCORE = 0.55   # overall floor below which we refuse to guess ("did you mean")
_AMBIG_FIRST = 0.60  # first-name confidence below which a multi-candidate result
                     # is flagged ambiguous rather than returned as certain


def _concat_dedupe(*frames: pd.DataFrame) -> pd.DataFrame:
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "key_mlbam" in df.columns:
        df = df.drop_duplicates(subset=["key_mlbam"]).reset_index(drop=True)
    return df


def resolve_player(player_name: str) -> ResolveResult | None:
    """
    Resolve a player name to IDs. Returns None when there is no confident match —
    returning the WRONG player is worse than failing, so a low-confidence result
    becomes a clean "not found / did you mean" instead.

    If multiple plausible matches exist, returns the best one plus alternatives.
    """
    if not player_name or not player_name.strip():
        return None

    cache_key = cache.make_key("resolve_player", player_name.lower().strip())
    hit = cache.get(cache_key)
    if hit is not None:
        return hit

    last, first = _parse_name(player_name)

    # Build the candidate pool. Critically, when a first name is given we ALSO pull
    # the full last-name cohort. pybaseball's (last, first) fuzzy lookup returns the
    # "most similar" names when there's no exact hit — and for nickname cases (e.g.
    # "Michael King" is registered as "Mike King") those can be entirely wrong-last-
    # name players ("Michael Tonkin"...). The last-name cohort guarantees the real
    # same-last-name players are in the pool to be scored and anchored against.
    df = _lookup_raw(last, first)
    if first:
        df = _concat_dedupe(df, _lookup_raw(last, ""))
    if df is None or df.empty:
        df = _lookup_raw(last, "")
    if df is None or df.empty:
        return None

    input_last = last.lower().strip()
    input_first = first.lower().strip()
    full_input = player_name.strip().lower()

    df = df.copy()
    norm_input_last = _norm_last(input_last)
    df["_last_score"] = df.apply(
        lambda r: _similarity(norm_input_last, _norm_last(str(r.get("name_last", "")))), axis=1
    )
    df["_first_score"] = df.apply(
        lambda r: _first_name_score(input_first, str(r.get("name_first", ""))), axis=1
    )

    # Last-name anchoring: when the user supplied a last name, only trust
    # candidates whose last name actually matches it. THIS is what stops
    # "Michael King" from resolving to "Michael Tonkin" — Tonkin's last name
    # doesn't clear the gate, so he's never a candidate. If nothing clears the
    # gate we're not confident on the last name at all → refuse to guess.
    if input_last:
        anchored = df[df["_last_score"] >= _LAST_GATE]
        if anchored.empty:
            logger.info("No confident last-name match for '%s' — refusing to guess.", player_name)
            return None
        df = anchored.copy()

    def total(row: pd.Series) -> float:
        fn = str(row.get("name_first", "")).lower()
        ln = str(row.get("name_last", "")).lower()
        full_score = max(
            _similarity(full_input, f"{fn} {ln}"),
            _similarity(full_input, f"{ln} {fn}"),
        )
        # Last name dominates (it's the reliable key); first name is the tiebreaker
        # among the same-last cohort; full-string is a light bonus for exact hits.
        return 0.50 * row["_last_score"] + 0.35 * row["_first_score"] + 0.15 * full_score

    df["_score"] = df.apply(total, axis=1)
    # Recency as a STRICT secondary key: it only breaks exact score ties (e.g. two
    # players with the identical name like "Jose Ramirez"), so the active player
    # wins over a retired one. It can never override a real score difference.
    df["_recency"] = df.apply(lambda r: _safe_int(r.get("mlb_played_last")) or 0, axis=1)
    df = df.sort_values(["_score", "_recency"], ascending=False).reset_index(drop=True)

    best_score = float(df.iloc[0]["_score"])
    if best_score < _MIN_SCORE:
        return None

    players: list[PlayerID] = []
    seen: set = set()
    for i in range(len(df)):
        if float(df.iloc[i]["_score"]) < _MIN_SCORE:
            break
        pid = _to_player_id(df.iloc[i])
        if pid["mlbam_id"] in seen:
            continue
        seen.add(pid["mlbam_id"])
        players.append(pid)
        if len(players) >= 5:
            break
    if not players:
        return None

    best_first = float(df.iloc[0]["_first_score"])
    runner_up = float(df.iloc[1]["_score"]) if len(df) > 1 else 0.0

    def _norm_full(p: PlayerID) -> str:
        return _strip_accents(p["name_display"]).lower().strip()

    # Two+ candidates sharing the SAME full name (e.g. multiple "Jose Ramirez") is
    # inherently ambiguous even though the first name matched exactly — surface the
    # alternatives so the caller can disambiguate (we still default to the active one).
    top_norm = _norm_full(players[0])
    dup_exact_name = sum(1 for p in players if _norm_full(p) == top_norm) > 1

    # Otherwise ambiguous when there are real alternatives AND we're not confident:
    # a tight race for the top spot, or weak first-name evidence (last-name-only
    # query, or no nickname/exact first-name match).
    ambiguous = len(players) > 1 and (
        dup_exact_name
        or (best_first < 0.95 and ((best_score - runner_up) < 0.08 or best_first < _AMBIG_FIRST))
    )

    result = ResolveResult(
        player=players[0],
        alternatives=players[1:],
        ambiguous=ambiguous,
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
