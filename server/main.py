"""
StatsDeck MCP Server — FastMCP entry point.

Transport: MCP_TRANSPORT=stdio (default, for Claude Desktop)
           MCP_TRANSPORT=http  (Streamable HTTP — Railway / remote deploy)

Auth modes (HTTP only):
  Clerk OAuth  — set CLERK_DOMAIN + CLERK_OAUTH_CLIENT_ID + CLERK_OAUTH_CLIENT_SECRET
                 + MCP_SERVER_URL. Preferred; supports Claude's "Connect" flow with no
                 manual credential entry.

                 OAuth discovery chain:
                   GET  MCP_SERVER_URL/.well-known/oauth-protected-resource
                     → authorization_servers: [MCP_SERVER_URL]  (our server, not Clerk)
                   GET  MCP_SERVER_URL/.well-known/oauth-authorization-server
                     → issuer: MCP_SERVER_URL
                       authorization_endpoint: Clerk's real endpoint (from OIDC discovery)
                       token_endpoint:         Clerk's real endpoint
                       registration_endpoint:  MCP_SERVER_URL/oauth/register  (our DCR shim)
                   POST MCP_SERVER_URL/oauth/register
                     → returns pre-registered Clerk OAuth app credentials (201)
                 JWT tokens are issued by Clerk and validated by ClerkTokenVerifier.

  Legacy token — set MCP_AUTH_TOKEN only. Static bearer token; kept for transition period.
"""

import logging
import os
import sys
from datetime import date, timedelta
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources import mlb_stats, savant
from sources.player_resolver import resolve_player
from sources.profile import (
    DEFAULT_USER,
    current_user_id,
    get_current_profile,
    save_current_profile,
    get_profile,
    save_profile,
    profile_summary,
    key_hitting_cats,
    key_pitching_cats,
    is_daily_lineup,
    is_dynasty,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OAuth configuration — read at import time so FastMCP can be created with
# the right token_verifier.  All Clerk env vars must be set together.
# ---------------------------------------------------------------------------

_CLERK_DOMAIN = os.getenv("CLERK_DOMAIN", "").strip()
# Strip scheme if the env var was set with https:// included — all call sites construct
# their own https:// URLs from this value, so it must be a bare domain.
if _CLERK_DOMAIN.startswith(("https://", "http://")):
    _CLERK_DOMAIN = _CLERK_DOMAIN.split("://", 1)[1]
_CLERK_CLIENT_ID = os.getenv("CLERK_OAUTH_CLIENT_ID", "").strip()
_CLERK_CLIENT_SECRET = os.getenv("CLERK_OAUTH_CLIENT_SECRET", "").strip()
_MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "").strip().rstrip("/")
# Prepend https:// if the value was set without a scheme (common Railway copy-paste mistake)
if _MCP_SERVER_URL and not _MCP_SERVER_URL.startswith(("https://", "http://")):
    _MCP_SERVER_URL = "https://" + _MCP_SERVER_URL

_OAUTH_ENABLED = bool(_CLERK_DOMAIN and _CLERK_CLIENT_ID and _MCP_SERVER_URL)

# ---------------------------------------------------------------------------
# Transport security (DNS-rebinding protection on the StreamableHTTP transport).
# The MCP transport validates the incoming Host header against an allow-list;
# an unlisted host yields "421 Misdirected Request".  We derive the allowed host
# from MCP_SERVER_URL automatically and allow an env-driven override list so a
# future custom domain needs no code change.
# ---------------------------------------------------------------------------

from urllib.parse import urlsplit as _urlsplit

_allowed_hosts: list[str] = []
if _MCP_SERVER_URL:
    _server_host = _urlsplit(_MCP_SERVER_URL).netloc  # host[:port], scheme already stripped
    if _server_host:
        _allowed_hosts.append(_server_host)
# Extra hosts (comma-separated) — e.g. a custom domain added later in Railway.
_allowed_hosts += [
    h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
]
# Localhost for local stdio/HTTP testing (":*" allows any port).
_allowed_hosts += ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
# De-dupe while preserving order.
_allowed_hosts = list(dict.fromkeys(_allowed_hosts))

# Origins: allow our own server URL and the localhost dev origins.
_allowed_origins: list[str] = []
if _MCP_SERVER_URL:
    _allowed_origins.append(_MCP_SERVER_URL)
_allowed_origins += [
    o.strip() for o in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
]
_allowed_origins += [
    "http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*",
]
_allowed_origins = list(dict.fromkeys(_allowed_origins))

_INSTRUCTIONS = (
    "You are StatsDeck, an expert fantasy baseball assistant with access to live MLB data. "
    "You actively guide users toward smart roster decisions — not just returning raw stats, "
    "but framing them in terms of fantasy value, regression, and league context. "
    "Statcast data (barrel rate, xwOBA, exit velocity) comes ONLY from Baseball Savant. "
    "Game logs, season stats, probable pitchers, and IL data come from the MLB Stats API. "
    "Always note the data source and any caveats (sample size, data age). "
    "When suggestions are present in a tool response, surface them to guide the user's next step."
)

if _OAUTH_ENABLED:
    from server.oauth import ClerkTokenVerifier
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.transport_security import TransportSecuritySettings
    from pydantic import AnyHttpUrl

    logger.info(
        "OAuth mode: CLERK_DOMAIN=%s MCP_SERVER_URL=%s allowed_hosts=%s",
        _CLERK_DOMAIN, _MCP_SERVER_URL, _allowed_hosts,
    )
    mcp = FastMCP(
        "StatsDeck — MLB Fantasy Assistant",
        instructions=_INSTRUCTIONS,
        token_verifier=ClerkTokenVerifier(_CLERK_DOMAIN, _CLERK_CLIENT_ID),
        auth=AuthSettings(
            # Must point to OUR server, not Clerk's domain.
            # FastMCP puts this in the PRM as authorization_servers, so Claude fetches
            # /.well-known/oauth-authorization-server from us — where our DCR shim lives.
            # Clerk's domain belongs only in ClerkTokenVerifier, not here.
            issuer_url=AnyHttpUrl(_MCP_SERVER_URL),
            resource_server_url=AnyHttpUrl(_MCP_SERVER_URL),
        ),
        transport_security=TransportSecuritySettings(
            allowed_hosts=_allowed_hosts,
            allowed_origins=_allowed_origins,
        ),
    )
else:
    if _CLERK_DOMAIN:
        logger.warning(
            "CLERK_DOMAIN is set but CLERK_OAUTH_CLIENT_ID or MCP_SERVER_URL is missing — "
            "OAuth disabled. Falling back to static token or no auth."
        )
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        "StatsDeck — MLB Fantasy Assistant",
        instructions=_INSTRUCTIONS,
        transport_security=TransportSecuritySettings(
            allowed_hosts=_allowed_hosts,
            allowed_origins=_allowed_origins,
        ),
    )


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_user_id() -> str:
    """
    Return the current user's ID from the validated OAuth token, or DEFAULT_USER
    for stdio transport / unauthenticated requests.
    Also sets the profile context var so prompts and helpers pick it up.
    """
    token = get_access_token()
    uid = (token.subject if token and token.subject else DEFAULT_USER)
    current_user_id.set(uid)
    return uid


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

_GAMES_LIMIT = 50   # max game-log entries per tool response (~25 k tokens safe)
_IL_LIMIT = 40      # max IL entries per team in all-teams query


def _truncate_list(items: list, limit: int, label: str = "items") -> tuple[list, str | None]:
    """Return (truncated_list, truncation_note | None)."""
    if len(items) <= limit:
        return items, None
    return items[:limit], f"Showing {limit} of {len(items)} {label}. Request a shorter window for full data."


def _err(msg: str) -> dict:
    return {"success": False, "error": msg, "data": {}, "source": "", "suggestions": []}


def _wrap(fn, *args, suggester=None, **kwargs) -> dict:
    """
    Call a data-layer function. Returns structured response:
      { success, source, data, suggestions }
    Exceptions become clean error messages — never stack traces.
    """
    try:
        raw = fn(*args, **kwargs)
        source = raw.get("source", "")
        data = {k: v for k, v in raw.items() if k != "source"}
        suggs = suggester(data) if suggester else []
        return {"success": True, "source": source, "data": data, "suggestions": suggs}
    except ValueError as exc:
        return _err(str(exc))
    except RuntimeError as exc:
        return _err(f"Data fetch failed: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in tool call")
        return _err(f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Suggestion generators — fantasy-expertise rules applied to returned data
# ---------------------------------------------------------------------------

def _suggest_statcast(data: dict) -> list[str]:
    m = data.get("metrics", {})
    if not m:
        return []
    suggestions: list[str] = []
    xwoba = m.get("xwoba")
    barrel = m.get("barrel_rate")
    hh = m.get("hard_hit_rate")
    player = data.get("player", "This player")

    if xwoba is not None and barrel is not None:
        if xwoba >= 0.370 and barrel >= 0.08:
            suggestions.append(
                f"Elite contact profile (xwOBA {xwoba:.3f}, barrel {barrel:.1%}). "
                "If counting stats are lagging, this is a textbook buy-low candidate — "
                "use the buy_low_finder prompt."
            )
        elif xwoba <= 0.275:
            suggestions.append(
                f"Weak underlying contact (xwOBA {xwoba:.3f}). Surface stats may be outrunning "
                "actual quality — check sell_high_finder before extending roster commitment."
            )

    if barrel is not None and barrel >= 0.12:
        suggestions.append(
            f"Barrel rate {barrel:.1%} is elite (top ~10% of MLB). "
            "Power upside is real regardless of recent HR count. "
            "Compare to your current option with compare_players."
        )

    if hh is not None and hh >= 0.50:
        suggestions.append(
            f"Hard-hit rate {hh:.1%} — contact quality is excellent. "
            "Pair with get_player_recent to see if results are matching the quality."
        )

    if not suggestions:
        suggestions.append(
            "Use compare_players to benchmark against a roster alternative, "
            "or buy_low_finder / sell_high_finder if you're making a trade decision."
        )
    return suggestions


def _suggest_recent(data: dict) -> list[str]:
    games = data.get("games_played", 0)
    if games == 0:
        return ["No games found in this window. Try a longer period or confirm the player is active."]
    suggestions: list[str] = []
    if games < 5:
        suggestions.append(
            f"Only {games} games — small sample. "
            "Use get_player_statcast to see underlying contact quality before a roster decision."
        )
    else:
        suggestions.append(
            "Use get_player_statcast to validate whether this form is real (good underlying metrics) "
            "or variance (hot BABIP, soft contact getting through)."
        )
    return suggestions


def _suggest_season_stats(data: dict) -> list[str]:
    return [
        "Season stats are context — use get_player_statcast for underlying quality "
        "(are these stats sustainable?) and get_player_recent for current form (hot or cold right now?)."
    ]


def _suggest_pitchers(data: dict) -> list[str]:
    count = data.get("game_count", 0)
    suggestions = [
        "Use the streaming_pitchers prompt for a full week analysis: "
        "it ranks starters by opponent quality, park factors, and recent form."
    ]
    if count == 0:
        suggestions.insert(0, "No games found for this date — try an adjacent date or check if it's an off day.")
    return suggestions


def _suggest_bvp(data: dict) -> list[str]:
    matchup = data.get("matchup", {})
    pas = matchup.get("plate_appearances", 0)
    if pas < 10:
        return [
            f"Only {pas} PAs in this matchup this season — insufficient for meaningful signal. "
            "Weight get_player_statcast and get_player_recent more heavily for tonight's decision."
        ]
    return [
        "Solid matchup sample. Combine with get_park_factors for the venue to complete the start/sit picture."
    ]


def _suggest_injuries(data: dict) -> list[str]:
    if data.get("on_injured_list"):
        return [
            "Player is on the IL. Use the waiver_targets prompt to find replacements that fit your categories."
        ]
    qt = data.get("query_type", "")
    if qt == "team":
        count = data.get("count", 0)
        if count >= 5:
            return [
                f"{count} players on IL for this team. "
                "Check which positions are depleted — roster instability can affect team offense/pitching quality."
            ]
    return []


def _suggest_park(data: dict) -> list[str]:
    rf = data.get("run_factor", 100)
    stadium = data.get("stadium", "This park")
    if rf >= 108:
        return [
            f"{stadium} is a hitter's park (run factor {rf}). "
            "Boost offensive projections for hitters playing here. "
            "Avoid streaming pitchers scheduled in this park — use streaming_pitchers to find better options."
        ]
    if rf <= 93:
        return [
            f"{stadium} suppresses offense (run factor {rf}). "
            "Temper hitter expectations; this is a favorable venue for streaming pitchers. "
            "Check streaming_pitchers for starters with home starts here."
        ]
    return [f"{stadium} is near-neutral (run factor {rf}). Park isn't a meaningful factor for this decision."]


def _suggest_compare(data: dict) -> list[str]:
    a = data.get("player_a", {})
    b = data.get("player_b", {})
    sc_a = a.get("statcast", {})
    sc_b = b.get("statcast", {})
    xw_a = sc_a.get("xwoba")
    xw_b = sc_b.get("xwoba")
    if xw_a and xw_b:
        diff = xw_a - xw_b
        if abs(diff) >= 0.030:
            better = a["name"] if diff > 0 else b["name"]
            return [
                f"{better} has meaningfully better underlying contact quality "
                f"(Δ xwOBA {abs(diff):.3f}). Weight this over short-term counting stats. "
                "Use trade_evaluator prompt if you're considering a trade between them."
            ]
    return [
        "Use trade_evaluator prompt for a full trade-framing analysis with "
        "category fit and rest-of-season outlook."
    ]


# ---------------------------------------------------------------------------
# Tools: league profile
# ---------------------------------------------------------------------------

@mcp.tool()
def set_league_profile(
    scoring_type: str,
    lineup_lock: str = "daily",
    league_size: int = 12,
    league_style: str = "redraft",
    hitting_categories: str = "R,HR,RBI,SB,AVG",
    pitching_categories: str = "W,SV,K,ERA,WHIP",
    roster_positions: str = "",
    bench_spots: int = 4,
    il_spots: int = 2,
    waiver_type: str = "faab",
    faab_budget: int = 100,
) -> dict:
    """
    Save your league settings so every prompt and tool can give personalized advice.
    Call this once; settings persist across conversations.

    Args:
        scoring_type: One of: roto_categories, h2h_categories, h2h_points, points
        lineup_lock: "daily" (can stream each day) or "weekly" (locked Sunday–Saturday)
        league_size: Number of teams (affects scarcity — 10-team vs 16-team plays very differently)
        league_style: "redraft", "keeper", or "dynasty"
        hitting_categories: Comma-separated hitting cats (e.g. "R,HR,RBI,SB,AVG,OBP")
        pitching_categories: Comma-separated pitching cats (e.g. "W,SV,K,ERA,WHIP,QS")
        roster_positions: Comma-separated positional slots (e.g. "C,1B,2B,3B,SS,OF,OF,OF,UTIL,SP,SP,SP,RP,RP")
        bench_spots: Number of bench slots
        il_spots: Number of IL/IR slots
        waiver_type: "faab" (blind auction) or "rolling" (first-come-first-served)
        faab_budget: Starting FAAB dollars (if waiver_type is faab)

    Returns:
        Confirmation with the stored profile summary.
    """
    valid_scoring = {"roto_categories", "h2h_categories", "h2h_points", "points"}
    if scoring_type not in valid_scoring:
        return _err(f"scoring_type must be one of: {', '.join(sorted(valid_scoring))}")
    if lineup_lock not in ("daily", "weekly"):
        return _err("lineup_lock must be 'daily' or 'weekly'")
    if league_style not in ("redraft", "keeper", "dynasty"):
        return _err("league_style must be 'redraft', 'keeper', or 'dynasty'")

    profile = {
        "scoring_type": scoring_type,
        "lineup_lock": lineup_lock,
        "league_size": league_size,
        "league_style": league_style,
        "categories": {
            "hitting": [c.strip().upper() for c in hitting_categories.split(",") if c.strip()],
            "pitching": [c.strip().upper() for c in pitching_categories.split(",") if c.strip()],
        },
        "roster": {
            "positions": [p.strip() for p in roster_positions.split(",") if p.strip()],
            "bench": bench_spots,
            "il_slots": il_spots,
        },
        "waivers": {
            "type": waiver_type,
            "faab_budget": faab_budget if waiver_type == "faab" else None,
        },
    }
    user_id = _get_user_id()
    save_profile(user_id, profile)
    logger.info("League profile saved for user_id=%s", user_id)
    return {
        "success": True,
        "source": "local profile storage",
        "data": {"message": "League profile saved.", "summary": profile_summary(profile)},
        "suggestions": [
            "Profile saved! Now try a guided workflow: weekly_lineup_review, buy_low_finder, "
            "or streaming_pitchers — each prompt will use your league settings automatically."
        ],
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_league_profile() -> dict:
    """
    Retrieve your stored league settings.

    Returns the profile you set with set_league_profile(), including scoring format,
    categories, roster construction, and waiver settings. All prompts read this
    automatically — you don't need to repeat it each conversation.

    Returns:
        Your league profile, or instructions to set one if not yet configured.
    """
    uid = _get_user_id()
    profile = get_profile(uid)
    if not profile:
        return {
            "success": True,
            "source": "local profile storage",
            "data": {"profile": None},
            "suggestions": [
                "No profile found. Call set_league_profile() to store your league settings. "
                "This unlocks personalized advice in every prompt and tool response."
            ],
        }
    return {
        "success": True,
        "source": "local profile storage",
        "data": {"profile": profile, "summary": profile_summary(profile)},
        "suggestions": [
            "Profile loaded. Use weekly_lineup_review, buy_low_finder, or streaming_pitchers "
            "for expert guided workflows tuned to your league."
        ],
    }


# ---------------------------------------------------------------------------
# Tool: how_to_use
# ---------------------------------------------------------------------------

_HELP_TOPICS: dict[str, str] = {
    "buy low": (
        "**Buy-low analysis** — finding players whose underlying quality exceeds their results.\n\n"
        "Key signal: high xwOBA (≥ .360) or high barrel rate (≥ 10%) paired with weak counting stats or AVG. "
        "This means the player is making hard contact but results haven't caught up yet — positive regression "
        "is likely.\n\n"
        "**Tools to use:**\n"
        "- `get_player_statcast(name, days=21)` — pull xwOBA, barrel rate, hard-hit%\n"
        "- `get_player_season_stats(name)` — compare expected quality to actual results\n"
        "- `get_player_recent(name, days=14)` — check if the slump is recent or ongoing\n\n"
        "**Fastest path:** use the `buy_low_finder` prompt — paste in a list of players you're targeting "
        "and it runs the full analysis with expert framing.\n\n"
        "**Don't confuse bad luck with a bad player.** A player with BOTH weak Statcast AND weak results "
        "is just struggling — not a buy-low."
    ),
    "sell high": (
        "**Sell-high analysis** — finding players outrunning their underlying metrics.\n\n"
        "Key signals: very high BABIP (> .340 is usually unsustainable), HR/FB rate spike without "
        "supporting barrel rate (> 20% HR/FB rarely persists), or strong AVG with weak xwOBA.\n\n"
        "**Tools to use:**\n"
        "- `get_player_statcast(name, days=21)` — pull xwOBA and barrel rate\n"
        "- `get_player_season_stats(name)` — check BABIP vs. career average\n"
        "- `get_player_recent(name, days=14)` — see if the hot streak is recent and potentially cooling\n\n"
        "**Fastest path:** `sell_high_finder` prompt — list the players you're considering trading away "
        "and it flags which ones are genuinely good vs. running hot.\n\n"
        "**Timing matters.** Sell while a player's name value is highest — before the regression shows "
        "in the box score."
    ),
    "streaming": (
        "**Streaming pitchers** — adding free-agent starters for a week then dropping them.\n\n"
        "**What makes a good streamer:**\n"
        "1. Favorable opponent (team with weak offense)\n"
        "2. Pitcher-friendly park (run factor < 96) — check `get_park_factors`\n"
        "3. Two starts in the week (doubles the value)\n"
        "4. Decent baseline (ERA < 4.50, WHIP < 1.35) — check `get_player_season_stats`\n"
        "5. Good recent form — check `get_player_recent`\n\n"
        "**Daily vs. weekly lineups matter a lot.** Daily leagues can stream aggressively — "
        "swap based on each day's matchup. Weekly leagues need higher confidence since you're "
        "committed for the full week.\n\n"
        "**Fastest path:** `streaming_pitchers` prompt — give it a date range and it surfaces "
        "the week's best options with all factors considered."
    ),
    "trades": (
        "**Evaluating trades** — don't judge a trade in a vacuum.\n\n"
        "**Framework:**\n"
        "1. **Current value** — who's producing more per game right now?\n"
        "2. **Sustainable quality** — whose Statcast metrics support continued production?\n"
        "3. **Category fit** — does this trade help your specific weaknesses or hurt your strengths?\n"
        "4. **Health** — always check IL status before accepting\n\n"
        "**Common mistake:** valuing season stats over underlying quality. A player with a .290 BA "
        "and .330 xwOBA is more valuable than one with a .305 BA and .270 xwOBA — the second "
        "player's results won't last.\n\n"
        "**Fastest path:** `trade_evaluator` prompt — paste in both sides and get a structured "
        "verdict on who wins current value, rest-of-season, and category fit.\n\n"
        "Set your league profile with `set_league_profile` first — trade advice is much sharper "
        "when the system knows your categories."
    ),
    "lineup": (
        "**Weekly lineup decisions** — start/sit and streaming.\n\n"
        "**Decision framework (in order of weight):**\n"
        "1. **Health** — check `get_injuries` first; don't start an IL candidate\n"
        "2. **Matchup** — pitcher quality and park factor matter more than short streaks\n"
        "3. **Current form** — recent hot streaks are real but don't override terrible matchups\n"
        "4. **Underlying metrics** — Statcast is the tiebreaker for borderline calls\n\n"
        "**For pitchers:** two-start weeks >>> one-start weeks for streaming. "
        "Check `get_probable_pitchers` for the full week first.\n\n"
        "**Fastest path:** `weekly_lineup_review` prompt — paste your roster and it works through "
        "form, matchups, and park factors to recommend starts/sits for the week."
    ),
    "waivers": (
        "**Waiver wire strategy** — ranking pickups by value.\n\n"
        "**Prioritize in this order:**\n"
        "1. Healthy (not on IL or day-to-day)\n"
        "2. Recent hot form with underlying Statcast support\n"
        "3. Favorable schedule for the next 2 weeks\n"
        "4. Positional need and category fit\n\n"
        "**Fastest path:** `waiver_targets` prompt — paste the available players from your waiver "
        "page and get a ranked list with reasoning.\n\n"
        "**FAAB tip:** bid on players with good underlying metrics + temporary bad results more "
        "aggressively — that's where you beat competitors who only watch the box score."
    ),
}

_GENERAL_HELP = """**StatsDeck — MLB Fantasy Assistant**

I give you live MLB data plus expert framing for fantasy decisions. Here's how to get the most out of me:

**Start here (one-time setup):**
→ Call `set_league_profile()` with your league settings (scoring type, categories, daily/weekly lineups). Every prompt uses this to personalize its advice.

**Guided workflows (best starting points):**
- `weekly_lineup_review` — full start/sit analysis for your roster; checks form, matchups, and park factors
- `buy_low_finder` — find players whose Statcast quality exceeds their results (positive regression candidates)
- `sell_high_finder` — find players outrunning their metrics (trade before regression hits)
- `streaming_pitchers` — rank this week's free-agent starters by matchup, park, and skill
- `trade_evaluator` — evaluate a trade on current value, underlying quality, and category fit
- `waiver_targets` — rank your waiver wire options by production, metrics, and schedule

**Individual tools (for specific questions):**
- "How has Aaron Judge been hitting lately?" → `get_player_recent`
- "What are Freddie Freeman's Statcast numbers?" → `get_player_statcast`
- "Who's pitching tonight?" → `get_probable_pitchers`
- "Is Mookie Betts on the IL?" → `get_injuries`
- "How hitter-friendly is Coors Field?" → `get_park_factors`
- "Ohtani vs. Judge this month?" → `compare_players`

**Example questions that work well:**
1. "My roster: [list players]. Help me set my lineup for this week."
2. "I'm considering trading away [player A] for [player B] — is that a good deal?"
3. "Who should I pick up on waivers? My options are: [list players]."
4. "Is [player]'s hot streak real or a BABIP mirage?"
5. "Who are good streaming pitchers this week?"

For topic-specific tips, call `how_to_use("buy low")`, `how_to_use("streaming")`, etc."""


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def how_to_use(topic: str = "") -> dict:
    """
    Get guidance on using StatsDeck — available workflows, example questions, and expert tips.

    Call with no arguments for a general orientation.
    Call with a topic for targeted guidance on that workflow.

    Args:
        topic: One of: "buy low", "sell high", "streaming", "trades", "lineup", "waivers"
               Leave blank for general orientation.

    Returns:
        Orientation text and/or topic-specific tips with which tools/prompts to use.
    """
    topic_key = topic.lower().strip()
    text = _HELP_TOPICS.get(topic_key)
    if topic_key and not text:
        # fuzzy match
        from difflib import get_close_matches
        matches = get_close_matches(topic_key, list(_HELP_TOPICS.keys()), n=1, cutoff=0.4)
        text = _HELP_TOPICS.get(matches[0]) if matches else None

    profile = get_current_profile()
    profile_note = (
        ""
        if profile
        else "\n\n⚠️ **Profile not set.** Call `set_league_profile()` to unlock personalized advice."
    )

    content = (text or _GENERAL_HELP) + profile_note

    return {
        "success": True,
        "source": "StatsDeck built-in guidance",
        "data": {"topic": topic_key or "general", "content": content},
        "suggestions": [
            "Call set_league_profile() if you haven't — it makes every prompt significantly more useful."
        ] if not profile else [],
    }


# ---------------------------------------------------------------------------
# Tool: get_player_season_stats
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_player_season_stats(player_name: str, season: int | None = None) -> dict:
    """
    Get a player's full season batting or pitching statistics from the MLB Stats API.

    Best used as **context and baseline**: season stats tell you where a player
    stands year-to-date, but they can mask hot/cold streaks and don't reveal
    *why* a player is performing that way. Pair with get_player_recent (current
    form) and get_player_statcast (underlying contact quality) for a full picture.

    Useful for:
    - Establishing a baseline before a trade evaluation
    - Checking ERA/WHIP/K-rate before streaming a pitcher
    - Comparing actual stats to Statcast expectations (is BA propped up by a high BABIP?)

    Args:
        player_name: Full player name (e.g. "Shohei Ohtani", "Spencer Strider")
        season: Season year — defaults to current season. Use prior years for historical context.

    Returns:
        Standard counting and rate stats. Pitchers get ERA/WHIP/K9; batters get
        AVG/OBP/SLG/OPS plus HR, RBI, SB, R. Source: MLB Stats API.
    """
    return _wrap(mlb_stats.get_player_season_stats, player_name, season,
                 suggester=_suggest_season_stats)


# ---------------------------------------------------------------------------
# Tool: get_player_recent
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_player_recent(player_name: str, days: int = 14) -> dict:
    """
    Get a player's game-by-game stats over the last N days — the primary
    signal for current form and hot/cold streak detection.

    **Fantasy use cases:**
    - **Start/sit timing:** A 10–14 day window captures momentum without
      over-indexing on a single good or bad game.
    - **Streaming decisions:** Check recent form before adding a pitcher
      off the wire — a pitcher with a 6+ ERA over the past two weeks is
      a streaming risk even with a favorable matchup.
    - **Trade timing:** Recent form drives perceived value. A player on a
      hot streak has maximum trade value right now; cold = potential buy-low.

    **Important:** always pair with get_player_statcast to determine
    whether a streak is real (good underlying metrics) or variance
    (hot BABIP, soft contact falling in).

    Args:
        player_name: Full player name
        days: Lookback window (default 14; 7 for short-term heat, 30 for broader trend)

    Returns:
        Per-game stat lines with date, opponent, home/away. Source: MLB Stats API.
    """
    if days < 1 or days > 90:
        return _err("days must be between 1 and 90")
    result = _wrap(mlb_stats.get_player_recent, player_name, days,
                   suggester=_suggest_recent)
    if result.get("success") and "games" in result.get("data", {}):
        games, note = _truncate_list(result["data"]["games"], _GAMES_LIMIT, "games")
        result["data"]["games"] = games
        if note:
            result["data"]["truncation_note"] = note
    return result


# ---------------------------------------------------------------------------
# Tool: get_player_statcast
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_player_statcast(player_name: str, days: int = 14) -> dict:
    """
    Get Statcast quality-of-contact metrics for a player over the last N days.
    THIS IS THE BUY-LOW / SELL-HIGH TOOL.

    Statcast measures *how well* a player is hitting, independent of whether
    results have followed. It's the difference between "is this player good?"
    and "is this player producing right now?".

    **Key metrics and what they mean:**
    - **xwOBA** (expected wOBA): predicted value based on exit velocity and
      launch angle. If xwOBA >> actual wOBA: positive regression candidate.
      If xwOBA << actual wOBA: overperformance, regression risk.
    - **Barrel rate**: % of batted balls hit with optimal EV (≥98 mph) and
      launch angle (26–30°). The purest power signal. ≥10% is plus, ≥12% is elite.
    - **Hard-hit rate**: % of batted balls ≥95 mph. ≥45% is excellent contact quality.
    - **Avg exit velocity**: raw contact quality. ≥92 mph is above average.

    **Baseball Savant is the ONLY source for Statcast data.**
    Responses are cached 3 hours to respect rate limits.

    Args:
        player_name: Full player name
        days: Lookback window (default 14; use 21–30 for more stable sample)

    Returns:
        Statcast metrics dict with source attribution. Source: Baseball Savant.
    """
    if days < 1 or days > 90:
        return _err("days must be between 1 and 90")
    return _wrap(savant.get_player_statcast, player_name, days,
                 suggester=_suggest_statcast)


# ---------------------------------------------------------------------------
# Tool: get_probable_pitchers
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_probable_pitchers(game_date: str | None = None) -> dict:
    """
    Get probable starting pitchers for every game on a given date.

    **Fantasy use cases:**
    - **Weekly planning:** Check the full week's slate on Monday to map out
      which hitters face tough vs. easy starters. A hitter facing a two-start
      ace twice this week may be a sit candidate.
    - **Two-start targets:** Run this for each day of the week to identify
      pitchers with two starts — automatically higher streaming priority.
    - **Daily streaming:** Check every morning to find hitters facing soft
      arms or pitchers with favorable matchups.

    For a full streaming analysis (opponent quality + park + pitcher skill),
    use the streaming_pitchers prompt instead of this tool alone.

    Args:
        game_date: Date in YYYY-MM-DD format. Defaults to today.
                   For weekly planning, call once per day of the upcoming week.

    Returns:
        All games with home/away teams and probable starters. "TBD" = not yet announced.
        Source: MLB Stats API.
    """
    if game_date:
        try:
            date.fromisoformat(game_date)
        except ValueError:
            return _err(f"Invalid date format '{game_date}'. Use YYYY-MM-DD.")
    return _wrap(mlb_stats.get_probable_pitchers, game_date,
                 suggester=_suggest_pitchers)


# ---------------------------------------------------------------------------
# Tool: get_batter_vs_pitcher
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_batter_vs_pitcher(batter: str, pitcher: str) -> dict:
    """
    Get head-to-head Statcast data for a specific batter vs. pitcher matchup.

    **Fantasy use cases:**
    - **Tonight's start/sit:** If your hitter is facing a specific arm tonight,
      pull the matchup history. Strong xwOBA in this matchup (even in small
      sample) is a positive signal.
    - **Platoon decisions:** Use this to confirm platoon advantages — does your
      lefty actually hit this righty well, or is the platoon split misleading?
    - **Streaming pitcher risk:** If you're streaming a pitcher, check that your
      opponent's key hitters don't have a history of crushing him.

    **Caveat:** Early in the season, sample sizes are very small. Under 10 PA,
    treat this as directional only — weight get_player_statcast and
    get_player_recent more heavily.

    Args:
        batter: Batter's full name (e.g. "Freddie Freeman")
        pitcher: Pitcher's full name (e.g. "Zack Wheeler")

    Returns:
        This-season Statcast matchup stats: PAs, outcomes, exit velocity, xwOBA.
        Source: Baseball Savant.
    """
    return _wrap(savant.get_batter_vs_pitcher, batter, pitcher,
                 suggester=_suggest_bvp)


# ---------------------------------------------------------------------------
# Tool: get_injuries
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_injuries(team_or_player: str | None = None) -> dict:
    """
    Get current injured list (IL) status for a team or player.

    **Fantasy use cases:**
    - **Before starting a player:** Confirm they're not day-to-day or IL-eligible.
      An unexpected IL placement mid-week can wreck your lineup.
    - **Waiver wire context:** A team with 4+ key players on IL may have
      attractive fill-in options with elevated playing time.
    - **Trade due diligence:** Always check IL status before accepting a trade —
      "acquiring" a player who's about to go on the 60-day IL is a common trap.

    Args:
        team_or_player: Team name or abbreviation (e.g. "Dodgers", "LAD"),
                        player name to check their specific status,
                        or omit for all teams (slow).

    Returns:
        For a team: all current IL placements with type (10-day, 60-day, etc.).
        For a player: whether they're on IL and the details.
        Source: MLB Stats API.
    """
    result = _wrap(mlb_stats.get_injuries, team_or_player,
                   suggester=_suggest_injuries)
    if result.get("success") and result.get("data", {}).get("query_type") == "team":
        il, note = _truncate_list(
            result["data"].get("injured_list", []), _IL_LIMIT, "IL entries"
        )
        result["data"]["injured_list"] = il
        if note:
            result["data"]["truncation_note"] = note
    elif result.get("success") and result.get("data", {}).get("query_type") == "all":
        # all-teams query: cap each team's list
        for team, entries in (result["data"].get("teams") or {}).items():
            if isinstance(entries, list) and len(entries) > _IL_LIMIT:
                result["data"]["teams"][team] = entries[:_IL_LIMIT]
    return result


# ---------------------------------------------------------------------------
# Tool: get_park_factors
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_park_factors(stadium_or_team: str) -> dict:
    """
    Get park factor data — how much a ballpark inflates or suppresses offense
    vs. league average (100 = neutral, 110 = 10% more runs than average).

    **Fantasy use cases:**
    - **Streaming decisions:** Never stream a pitcher at Coors Field or Great
      American Ball Park without very strong matchup context. A run factor of
      115 means ~15% more runs score there — it destroys ERA and WHIP.
    - **Hitter context:** A player posting big numbers at Coors may be getting
      a 10–15% boost. Adjust projections accordingly for road games.
    - **Two-start pitcher value:** A pitcher with one home start (pitcher's park)
      and one road start (hitter's park) may be less reliable than their line suggests.
    - **Waiver pickups:** All else equal, prefer hitters on teams that play
      half their home games in offense-friendly parks.

    Args:
        stadium_or_team: Stadium name (e.g. "Coors Field") or team abbreviation
                         (e.g. "COL"). Fuzzy matching handles common variations.

    Returns:
        Run factor, HR factor, and plain-English interpretation.
        Source: Baseball Savant (2024 data; live endpoint planned for v2).
    """
    return _wrap(savant.get_park_factors, stadium_or_team,
                 suggester=_suggest_park)


# ---------------------------------------------------------------------------
# Tool: compare_players
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def compare_players(player_a: str, player_b: str, days: int = 14) -> dict:
    """
    Compare two players side-by-side: recent form (MLB Stats API) + underlying
    contact quality (Baseball Savant Statcast). The go-to tool for start/sit
    tiebreakers and waiver wire decisions.

    **Fantasy use cases:**
    - **Start/sit tiebreaker:** Both players have similar counting stats but
      one has dramatically better Statcast numbers — start the one with
      underlying quality, not just recent luck.
    - **Waiver wire:** Should you drop player X for player Y? Compare both
      on recent form AND underlying metrics. A player on a cold streak with
      elite xwOBA is a hold; a hot player with 3% barrel rate is a sell-high.
    - **Pre-trade due diligence:** Quick side-by-side before committing to
      a trade. Use trade_evaluator prompt for deeper analysis with category fit.

    Args:
        player_a: First player's full name
        player_b: Second player's full name
        days: Lookback window in days (default 14)

    Returns:
        Side-by-side: games played, recent stat lines, Statcast metrics for both.
        Sources: MLB Stats API + Baseball Savant.
    """
    if days < 1 or days > 90:
        return _err("days must be between 1 and 90")

    result_a = _wrap(mlb_stats.get_player_recent, player_a, days)
    result_b = _wrap(mlb_stats.get_player_recent, player_b, days)
    statcast_a = _wrap(savant.get_player_statcast, player_a, days)
    statcast_b = _wrap(savant.get_player_statcast, player_b, days)

    _half = max(1, _GAMES_LIMIT // 2)  # each side gets half the total limit

    def _side(recent: dict, statcast: dict, name: str) -> dict:
        out: dict[str, Any] = {"name": name}
        if recent.get("success"):
            out["games_played"] = recent["data"].get("games_played", 0)
            games, note = _truncate_list(recent["data"].get("games", []), _half, "games")
            out["recent_games"] = games
            if note:
                out["games_truncation_note"] = note
            out["stat_group"] = recent["data"].get("stat_group", "")
        else:
            out["recent_error"] = recent.get("error", "unknown error")
        if statcast.get("success"):
            out["statcast"] = statcast["data"].get("metrics", {})
            out["statcast_note"] = statcast["data"].get("note", "")
        else:
            out["statcast_error"] = statcast.get("error", "unknown error")
        return out

    combined_data: dict[str, Any] = {
        "period_days": days,
        "player_a": _side(result_a, statcast_a, player_a),
        "player_b": _side(result_b, statcast_b, player_b),
    }
    suggestions = _suggest_compare(combined_data)
    return {
        "success": True,
        "source": "MLB Stats API + Baseball Savant (Statcast)",
        "data": combined_data,
        "suggestions": suggestions,
    }


# ---------------------------------------------------------------------------
# Tool: resolve_player_name
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def resolve_player_name(player_name: str) -> dict:
    """
    Resolve a player name to their MLBAM and FanGraphs IDs.

    Use this when a name lookup in another tool returns ambiguous or unexpected
    results. Disambiguates players with the same last name and handles common
    misspellings. Returns the best match plus up to 4 alternatives.

    Args:
        player_name: Any reasonable variant — "Ohtani", "Shohei Ohtani", "Freddie Freeman Jr."

    Returns:
        Best-match player with IDs, plus alternatives if name is ambiguous.
    """
    result = resolve_player(player_name)
    if result is None:
        return _err(f"No player found matching '{player_name}'. Try last name only or check spelling.")
    return {
        "success": True,
        "source": "pybaseball / Chadwick Bureau player register",
        "data": result,
        "suggestions": (
            [f"Ambiguous name. Using {result['player']['name_display']}. "
             f"Alternatives: {', '.join(p['name_display'] for p in result['alternatives'][:3])}. "
             "If the wrong player was returned, try a more specific name."]
            if result.get("ambiguous") else []
        ),
    }


# ---------------------------------------------------------------------------
# MCP Prompts — expert-guided workflows
# ---------------------------------------------------------------------------

@mcp.prompt()
def weekly_lineup_review(
    roster: str,
    week_start: str = "",
    week_end: str = "",
) -> str:
    """
    Full weekly lineup analysis: start/sit decisions with matchup, form, and park context.

    Args:
        roster: Comma-separated list of all players on your roster
        week_start: Start date in YYYY-MM-DD (optional, defaults to today)
        week_end: End date in YYYY-MM-DD (optional, defaults to 6 days out)
    """
    profile = get_current_profile()
    p_sum = profile_summary(profile)
    h_cats = key_hitting_cats(profile)
    p_cats = key_pitching_cats(profile)
    lock = (profile or {}).get("lineup_lock", "daily")
    lock_note = (
        "Your league uses **daily lineups** — you can swap players each day based on matchups."
        if lock == "daily"
        else "Your league uses **weekly lineups** — start/sit decisions lock for the whole week. "
             "Be conservative on borderline calls; you can't adjust mid-week."
    )

    if not week_start:
        week_start = date.today().isoformat()
    if not week_end:
        week_end = (date.today() + timedelta(days=6)).isoformat()

    return f"""Please run a full weekly lineup review for my fantasy baseball roster.

**League profile:** {p_sum}
**Week:** {week_start} through {week_end}
**{lock_note}**

**My roster:**
{roster}

Work through this step by step:

**Step 1 — Current form (call for each player):**
Call `get_player_recent(player_name, days=10)` for every player listed. Flag anyone with fewer than 3 hits over the past 10 days (cold) or 5+ multi-hit/multi-RBI games (hot).

**Step 2 — Weekly schedule:**
Call `get_probable_pitchers()` for {week_start} and each subsequent day through {week_end}. Map which pitchers each of my hitters faces. Note:
- Hitters facing two elite starters this week (sit candidates regardless of form)
- Hitters facing two weak arms (start candidates even if mildly cold)
- Any pitcher on my roster with two starts this week (automatic streaming consideration)

**Step 3 — Park factors for key matchups:**
For hitters with close start/sit decisions, call `get_park_factors(team)` for the stadiums they'll play in. Flag extreme parks — playing in Coors or Great American Ball Park is a boost; Petco or Oracle are headwinds.

**Step 4 — Validate cold streaks with Statcast:**
For any player on a meaningful cold streak (last 10 days), call `get_player_statcast(player_name, days=14)`. Determine whether the slump is:
- **Bad luck** (strong xwOBA ≥ .340, high barrel rate but poor results) → hold/start; regression coming
- **Real** (weak xwOBA < .290, low barrel rate) → bench this week; consider dropping

**Step 5 — Recommendations:**
Produce a scannable table or list:
| Player | Role | Start/Sit | Reason (1 sentence) |
|--------|------|-----------|---------------------|

Then:
- **Streaming targets:** 1–2 free-agent options to add for this week (pitchers or hitters with favorable schedule)
- **Injury alerts:** Anyone to monitor before locking a lineup
- **One actionable trade/waiver suggestion** if something obvious jumped out

Weight all decisions toward these categories — **hitting: {', '.join(h_cats)} | pitching: {', '.join(p_cats)}**."""


@mcp.prompt()
def buy_low_finder(players: str) -> str:
    """
    Identify positive regression candidates — players whose contact quality exceeds their results.

    Args:
        players: Comma-separated list of players to evaluate (your roster, targets, or anyone you're curious about)
    """
    profile = get_current_profile()
    p_sum = profile_summary(profile)
    h_cats = key_hitting_cats(profile)

    return f"""Please run a buy-low analysis for these players: {players}

**League profile:** {p_sum}
**My hitting categories:** {', '.join(h_cats)}

The goal is to find players whose *underlying contact quality* is strong but whose *surface results are lagging* — positive regression candidates worth acquiring or holding through the slump.

**For each player, pull:**
1. `get_player_statcast(player_name, days=21)` — the core signal: xwOBA, barrel rate, hard-hit%
2. `get_player_season_stats(player_name)` — actual results to compare against expected quality
3. `get_player_recent(player_name, days=14)` — is the slump recent or a longer pattern?

**Buy-low signals to look for:**
- **xwOBA ≥ .360 but weak counting stats or batting average** → hard contact not producing results yet; positive regression likely
- **Barrel rate ≥ 10% but HR total below pace** → power is there, luck hasn't been
- **Hard-hit rate ≥ 45% but weak AVG** → line drives and hard grounders falling for outs; BABIP should normalize up
- **Strong xwOBA but hot BABIP recently declining** → results were ahead, now the curve is evening out, metrics are the floor

**Not a buy-low (do NOT recommend acquiring):**
- Weak xwOBA AND weak results → bad player, not unlucky player
- Small sample (< 30 PA in Statcast window) → insufficient signal, treat as speculative only

**For each player, provide:**
1. The specific gap: *"He's hitting the ball at a 91 mph average exit velocity and 11% barrel rate, but his xwOBA (.375) is running 70 points above his actual AVG. The contact is there; the results will follow."*
2. A rating: **Strong Buy** / **Speculative Buy** / **Hold** / **Sell**
3. How they help my specific categories: {', '.join(h_cats)}
4. If they're a Strong Buy — what's a reasonable trade offer or FAAB bid range?

Rank by strength of buy-low signal, strongest first."""


@mcp.prompt()
def sell_high_finder(players: str) -> str:
    """
    Identify negative regression candidates — players outrunning their underlying metrics.

    Args:
        players: Comma-separated list of players to evaluate
    """
    profile = get_current_profile()
    p_sum = profile_summary(profile)
    h_cats = key_hitting_cats(profile)

    return f"""Please run a sell-high analysis for these players: {players}

**League profile:** {p_sum}
**My hitting categories:** {', '.join(h_cats)}

The goal is to find players whose *surface results are outrunning their underlying quality* — overperformance driven by luck rather than sustainable skill. These are players to trade away while their name value is highest, before regression shows in the box score.

**For each player, pull:**
1. `get_player_statcast(player_name, days=21)` — check if the quality supports the results
2. `get_player_season_stats(player_name)` — full season context, especially BABIP and HR/FB rate
3. `get_player_recent(player_name, days=14)` — is this a recent hot streak or sustained performance?

**Sell-high signals to look for:**
- **Low xwOBA (≤ .310) but strong batting average** → BABIP propping up results; this is luck, not skill
- **Low barrel rate (≤ 5%) but strong HR total** → HR/FB rate likely above 20%, almost always unsustainable
- **Weak hard-hit rate (≤ 35%) but hot recent form** → soft contact finding holes; will stop
- **Very high BABIP (> .340) without corresponding elite Statcast metrics** → regression target
- **Small-sample power spike** — 8+ HR in first 100 PA without barrel rate ≥ 8% to support it

**Not a sell-high:**
- High xwOBA AND strong results → legitimately good player, don't sell
- A player who changed something real (new stance, mechanical adjustment, position switch) may have genuinely improved — look for sustaining Statcast metrics

**For each player, provide:**
1. The specific overperformance: *"He's hitting .330 but his xwOBA is .270, his barrel rate is 3%, and his BABIP is .380. This is all luck — he'll regress to a .265 hitter."*
2. A rating: **Strong Sell** / **Sell if price is right** / **Hold** / **Don't sell (legitimately good)**
3. Suggested trade targets to request in return — who you want that you could plausibly get for an overperforming player
4. Urgency: how many more weeks before the regression likely shows?

Rank by urgency — the most-likely-to-crash players first."""


@mcp.prompt()
def streaming_pitchers(
    start_date: str = "",
    end_date: str = "",
    priorities: str = "balanced",
) -> str:
    """
    Rank free-agent starting pitchers for streaming during the given week.

    Args:
        start_date: YYYY-MM-DD (defaults to today)
        end_date: YYYY-MM-DD (defaults to 6 days out)
        priorities: What to optimize for — "strikeouts", "ratios" (ERA/WHIP), "wins", or "balanced"
    """
    profile = get_current_profile()
    p_sum = profile_summary(profile)
    p_cats = key_pitching_cats(profile)
    daily = is_daily_lineup(profile)

    if not start_date:
        start_date = date.today().isoformat()
    if not end_date:
        end_date = (date.today() + timedelta(days=6)).isoformat()

    lock_strategy = (
        "**Daily lineups:** Stream aggressively. One-start streamers with strong matchups are fine — "
        "you can swap daily. Prioritize upside (strikeout pitchers, good parks) over safety."
        if daily
        else "**Weekly lineups:** Be conservative. Favor two-start pitchers and safe-ratio anchors. "
             "One-start punt starts carry too much risk when you're locked in all week."
    )

    return f"""Please identify the best streaming pitcher options for the week of {start_date} through {end_date}.

**League profile:** {p_sum}
**Pitching categories:** {', '.join(p_cats)}
**Priority:** {priorities}
{lock_strategy}

**Step-by-step workflow:**

**Step 1 — Map the week's starters:**
Call `get_probable_pitchers()` for each day from {start_date} to {end_date}. Build a list of all starters, noting:
- Which pitchers make **two starts** this week (automatic streaming priority, nearly always worth rostering)
- Which pitchers face weaker offenses both starts vs. facing one tough lineup

**Step 2 — Park factors:**
For each candidate's start(s), call `get_park_factors(team)` for the venue. Eliminate or downgrade any pitcher starting in:
- Coors Field (COL) — automatic avoid for ERA/WHIP streamers
- Great American Ball Park (CIN) — near-avoid
- Any park with run factor > 108

Prioritize pitchers starting in: Petco Park (SD), Oracle Park (SF), T-Mobile Park (SEA), or any park with run factor < 95.

**Step 3 — Pitcher quality check:**
For the top 8–10 candidates remaining, call:
- `get_player_season_stats(pitcher_name)` — confirm ERA < 4.50, WHIP < 1.35 as baseline
- `get_player_recent(pitcher_name, days=14)` — flag anyone on a recent bad run (e.g., 2+ starts with 4+ ER)

**Step 4 — Rank and recommend:**

Structure your output in three tiers:

| Tier | Pitcher | Starts | Opponent(s) | Park Factor(s) | Why Stream |
|------|---------|--------|-------------|----------------|------------|
| **1 — Stream confidently** | ... | 2 | vs. weak teams | < 98 | Two starts, good matchups, solid recent form |
| **2 — Speculative stream** | ... | 1 | vs. mediocre team | neutral | One strong start, worth the roll |
| **3 — Avoid** | ... | 1 | vs. powerful offense | > 107 | Park + opponent too risky |

After the table:
- **Top pick** with a 2–3 sentence case
- **FAAB/waiver priority** if multiple two-start options are available
- Any **injury/roster risks** to watch before adding (check `get_injuries` if unsure)

Optimize toward: **{priorities}** and **{', '.join(p_cats)}**."""


@mcp.prompt()
def trade_evaluator(giving_up: str, getting: str) -> str:
    """
    Evaluate a trade offer across current value, underlying quality, and category fit.

    Args:
        giving_up: Comma-separated list of players you would send away
        getting: Comma-separated list of players you would receive
    """
    profile = get_current_profile()
    p_sum = profile_summary(profile)
    h_cats = key_hitting_cats(profile)
    p_cats = key_pitching_cats(profile)
    dynasty = is_dynasty(profile)
    style = (profile or {}).get("league_style", "redraft")

    all_players = [p.strip() for p in f"{giving_up},{getting}".split(",") if p.strip()]
    dynasty_note = (
        "\n**Dynasty/keeper context:** Factor in age, prospect pedigree, years of control, "
        "and multi-year value — not just current production."
        if dynasty
        else "\n**Redraft context:** Focus entirely on production for the rest of this season."
    )

    return f"""Please evaluate this trade offer for my fantasy baseball team.

**I am giving up:** {giving_up}
**I am receiving:** {getting}

**League profile:** {p_sum}
**Hitting categories I care about:** {', '.join(h_cats)}
**Pitching categories I care about:** {', '.join(p_cats)}{dynasty_note}

**Pull data for every player on both sides ({', '.join(all_players)}):**

For each player:
1. `get_player_season_stats(player_name)` — year-to-date production and baseline
2. `get_player_recent(player_name, days=14)` — current form going into the trade
3. `get_player_statcast(player_name, days=21)` — underlying quality (is the production sustainable?)
4. `get_injuries(player_name)` — **always check before accepting** — never accept an imminent IL candidate

**Three-lens trade evaluation:**

**Lens 1: Current value (next 4–6 weeks)**
Who is producing more right now, per game? Account for games played, plate appearances, and recent counting stats. State which side wins on immediate production.

**Lens 2: Underlying quality (sustainability)**
Compare xwOBA, barrel rate, and hard-hit% across both sides. The player with better Statcast metrics is more likely to maintain production. Call out any major discrepancy — a player with .310 xwOBA but strong counting stats is a regression risk you'd be acquiring.

**Lens 3: Category fit**
Map each player's contributions to **{', '.join(h_cats + p_cats)}**. Specifically:
- Does this trade improve your weakest categories?
- Does it create a new hole in a category you're currently winning?
- Are there any counting-stat categories (SB, HR, SV) where one side is clearly stronger?

**Verdict (required — be direct):**

| Dimension | Winner | Margin |
|-----------|--------|--------|
| Current value | [Side] | [Slight/Clear/Large] |
| Sustainable quality | [Side] | [Slight/Clear/Large] |
| Category fit for my team | [Side] | [Slight/Clear/Large] |

**Final recommendation:** Accept / Counter (explain what counter) / Decline

**One-sentence summary:** *Why* you recommend what you recommend. Not "it depends" — give a direct answer based on the data."""


@mcp.prompt()
def waiver_targets(available_players: str, roster_needs: str = "") -> str:
    """
    Rank waiver wire pickups by recent production, Statcast quality, schedule, and fit.

    Args:
        available_players: Comma-separated list of players available on your waiver wire
        roster_needs: Optional description of what you need (e.g. "need SB, weak at 3B, want SP")
    """
    profile = get_current_profile()
    p_sum = profile_summary(profile)
    h_cats = key_hitting_cats(profile)
    p_cats = key_pitching_cats(profile)
    daily = is_daily_lineup(profile)

    players = [p.strip() for p in available_players.split(",") if p.strip()]
    needs_line = f"\n**My roster needs:** {roster_needs}" if roster_needs else ""
    faab_note = (
        "**FAAB tip:** Bid higher on players with strong Statcast metrics despite recent cold streaks — "
        "your competition is looking at box scores, you're looking at underlying quality."
    ) if (profile or {}).get("waivers", {}).get("type") == "faab" else ""

    return f"""Please rank these waiver wire options for my fantasy team.

**Available players:** {available_players}

**League profile:** {p_sum}
**Hitting categories:** {', '.join(h_cats)}
**Pitching categories:** {', '.join(p_cats)}{needs_line}

{"**Daily lineups** — you can add and drop aggressively based on matchups. Streaming value counts." if daily else "**Weekly lineups** — prioritize players you want long-term or for a full week. Less speculative adds."}
{faab_note}

**Gather data for each player ({', '.join(players[:8])}{' and others' if len(players) > 8 else ''}):**

1. `get_player_recent(player_name, days=14)` — recent form (highest weight)
2. `get_player_statcast(player_name, days=21)` — is the form backed by real contact quality?
3. `get_player_season_stats(player_name)` — full season context (not just a hot week)
4. `get_injuries(player_name)` — confirm healthy and active before ranking highly
5. For pitchers: `get_probable_pitchers()` for upcoming starts, `get_park_factors(team)` for their home park

**Ranking criteria (apply in this order):**
1. ✅ **Healthy** — skip anyone on IL or day-to-day
2. 🔥 **Recent form backed by Statcast** — hot streak + strong xwOBA/barrel rate = add aggressively
3. 📈 **Buy-low opportunity** — cold streak but strong Statcast = add before the rebound
4. 📅 **Upcoming schedule** — favorable matchups / pitcher-friendly home park in next 2 weeks
5. 🎯 **Category fit** — how specifically do they help {', '.join((h_cats + p_cats)[:4])}?
{"6. 🏟️ **Roster need match** — " + roster_needs if roster_needs else ""}

**Output format:**

**Tier 1 — Add immediately (strong add):**
- Player name | Position | Why: [2-sentence case with specific numbers]

**Tier 2 — Speculative add (worth rostering if slots available):**
- Player name | Position | Why: [1-sentence case with the key stat]

**Tier 3 — Skip / avoid:**
- Player name | Reason: [1 sentence]

After the rankings:
- **Drop candidate:** If I should drop someone from my roster to make room, which one and why?
- **Biggest upside add:** The one player here with the highest ceiling, even if floor is low"""


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Rejects all HTTP requests that don't carry 'Authorization: Bearer <token>'.
    The /health path is exempt so Railway's health checks always pass.

    To disable auth (not recommended in production): unset MCP_AUTH_TOKEN.
    """

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/health", "/healthz"):
            return Response(
                content='{"status":"ok"}',
                status_code=200,
                media_type="application/json",
            )
        auth = request.headers.get("Authorization", "")
        if not (auth.startswith("Bearer ") and auth[7:].strip() == self._token):
            return Response(
                content='{"error":"Unauthorized — provide Authorization: Bearer <MCP_AUTH_TOKEN>"}',
                status_code=401,
                media_type="application/json",
            )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()

    if transport != "http":
        logger.info("Starting MCP server (stdio)")
        mcp.run(transport="stdio")
        return

    port = int(os.getenv("PORT", os.getenv("MCP_PORT", "8000")))
    host = os.getenv("MCP_HOST", "0.0.0.0")
    app = mcp.streamable_http_app()

    if _OAUTH_ENABLED:
        # Clerk OAuth mode — add AS metadata bridge + DCR shim routes
        from server.oauth import build_oauth_routes
        for route in build_oauth_routes(
            clerk_domain=_CLERK_DOMAIN,
            server_url=_MCP_SERVER_URL,
            client_id=_CLERK_CLIENT_ID,
            client_secret=_CLERK_CLIENT_SECRET,
        ):
            app.routes.append(route)
        logger.info(
            "OAuth mode active — PRM at %s/.well-known/oauth-protected-resource "
            "AS metadata at %s/.well-known/oauth-authorization-server "
            "DCR shim at %s/oauth/register",
            _MCP_SERVER_URL, _MCP_SERVER_URL, _MCP_SERVER_URL,
        )
    else:
        # Legacy static-token mode (kept until OAuth is verified then remove)
        static_token = os.getenv("MCP_AUTH_TOKEN", "").strip()
        if static_token:
            app.add_middleware(_BearerAuthMiddleware, token=static_token)
            logger.warning(
                "Using legacy static bearer token. "
                "Set CLERK_DOMAIN + CLERK_OAUTH_CLIENT_ID + CLERK_OAUTH_CLIENT_SECRET "
                "+ MCP_SERVER_URL to upgrade to OAuth."
            )
        else:
            logger.warning("No auth configured — server is unauthenticated. Do not use in production.")

    logger.info("Starting MCP server (HTTP) on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
