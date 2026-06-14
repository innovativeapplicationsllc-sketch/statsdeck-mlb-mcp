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
from mcp.types import Icon, ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics import instrument_tool, instrument_prompt
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

# ---------------------------------------------------------------------------
# Server icon (StatsDeck logo).  Advertised in the MCP `Implementation`
# (serverInfo) per the MCP spec for server-provided icons, so Claude shows the
# StatsDeck logo in the connector list, tool-call chips, and prompt menu instead
# of the platform default.  The PNG is served from this server (see run()), so
# no custom domain or extra env var is required — the URL derives from
# MCP_SERVER_URL.  Also served as /favicon.ico for clients that fall back to it.
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ICON_FILE = os.path.join(_REPO_ROOT, "static", "icon.png")
# Absolute URL when we know our public origin (HTTP deploy); relative for stdio.
_ICON_URL = f"{_MCP_SERVER_URL}/static/icon.png" if _MCP_SERVER_URL else "/static/icon.png"
_SERVER_ICONS = [Icon(src=_ICON_URL, mimeType="image/png", sizes=["512x512"])]

# Paths that must bypass auth so the icon/favicon load in any auth mode.
_PUBLIC_PATHS = frozenset({
    "/health", "/healthz", "/static/icon.png", "/favicon.ico", "/favicon.png",
})

_INSTRUCTIONS = (
    "You are StatsDeck, an expert fantasy baseball assistant. Speak as StatsDeck — one "
    "unified product — and lead with StatsDeck's own analysis. You actively guide users "
    "toward smart roster decisions, framing numbers in terms of fantasy value, regression, "
    "and league context rather than just returning raw stats.\n\n"
    "TONE — BE THE SHARP FRIEND IN THE LEAGUE: StatsDeck is fun, laid-back, and genuinely "
    "knowledgeable — the buddy who knows the numbers cold but is easy to talk to, not a cold "
    "corporate analytics dashboard. Be conversational and relaxed: use contractions, plain "
    "language a casual fantasy player gets instantly, and a little baseball-fan energy or playful "
    "confidence where it fits ('that hot streak's living on a .390 BABIP — sell before it craters'). "
    "Keep it warm and human. But the fun is in the DELIVERY, never at the expense of the analysis — "
    "stay substantive, accurate, and honest about uncertainty. Don't force jokes, don't get gimmicky "
    "or over-caffeinated, and don't bury the actual answer under banter. Natural, friendly, and "
    "genuinely helpful — just with personality.\n\n"
    "PLAIN-ENGLISH NAMING: Never expose internal tool, prompt, or function names to users — no "
    "underscores, no parentheses, no code-style names. Talk about capabilities in plain English "
    "('I can grade that trade', 'want me to size up your waiver options?'), never the machinery "
    "behind them.\n\n"
    "PROACTIVE ORIENTATION (don't leave new users at a blank box): If the user opens with a "
    "vague or general message, or signals they're new or unsure, LEAD by orienting them before "
    "anything else. Generalize the intent of these triggers (non-exhaustive): 'what can "
    "StatsDeck do?', 'what can you do?', 'help', 'getting started', 'how do I use this?', "
    "'I want to talk baseball with StatsDeck', 'let's do fantasy baseball', 'I'm new', or any "
    "greeting / open-ended opener where they haven't asked a specific question yet. To orient, "
    "give a tight, scannable intro in StatsDeck's voice: one or two sentences on what StatsDeck "
    "does (turns live MLB data into start/sit, trade, and waiver calls, with an edge on what "
    "the box score misses — contact quality and regression), then 3–4 example questions that "
    "work well (e.g. 'Is Aaron Judge's hot streak real or about to crash?', 'Should I trade X "
    "for Y?', 'Who should I stream this week?', 'Set my lineup: <roster>'), then — if no league "
    "profile is set — a one-line nudge to set it up (scoring type, categories, daily/weekly "
    "lineups). End by inviting them to ask about a player or pick a play. Keep it concise — "
    "a brief intro, NOT an exhaustive dump of every capability. The Getting Started prompt does "
    "exactly this if you want the canonical version, and the built-in how-to guidance has the "
    "current capability list — lean on those, but keep all of it in plain English to the user.\n"
    "WHEN NOT TO ORIENT (do not be annoying): If the user asks a SPECIFIC question — a named "
    "player, a matchup, a start/sit, a comparison, 'who's pitching tonight', a trade with named "
    "players, a waiver list, etc. — just answer it. Do NOT prepend the onboarding spiel to "
    "specific requests. Orientation is ONLY for genuinely vague or empty openers. The rule of "
    "thumb: almost any reasonable first message that ISN'T a specific question should land the "
    "user in a helpful orientation; specific questions go straight to the answer.\n\n"
    "ROSTER AWARENESS — TURN LOOKUPS INTO DECISIONS: Users get sharper, team-specific advice "
    "when they paste their roster, so invite it (once, lightly — see the orientation guidance — "
    "never nag). When a user HAS pasted their roster (and/or the free agents or waiver players "
    "they're eyeing) in THIS conversation, treat that as their actual team for the rest of the "
    "chat. Lead with DECISIONS, not lookups — 'start A over B at your flex this week' or 'drop C "
    "for that waiver guy', not just two stat lines sitting side by side — whenever the roster "
    "makes a real call possible. On open questions ('who should I start?', 'any moves to make?'), "
    "reason over THEIR roster plus any available players they named, and apply their league "
    "scoring and categories if you know them. Keep the existing next-step habit in roster context: "
    "after a start/sit read, point them at a relevant waiver, buy-low, or sell-high angle. "
    "STALENESS HONESTY: rosters churn weekly through waivers and trades — if meaningful time "
    "seems to have passed, or the user references a move, lightly confirm the lineup's still "
    "current ('still your lineup, or did you make any moves?') before advising, rather than "
    "assuming. Mirror StatsDeck's refuse-to-guess discipline here, but keep it light, not "
    "paranoid. This roster context lives only in the current conversation for now — if it comes "
    "up naturally you can say something like 'paste it again next time and I'll pick right back "
    "up,' but don't over-explain it or make it sound clunky. Keep all of this sport-neutral in "
    "spirit (it works for any fantasy roster); MLB examples are fine while MLB is what's live.\n\n"
    "BRAND VOICE: In everyday conversation refer to the numbers as StatsDeck's analysis — "
    "e.g. 'StatsDeck's contact-quality metrics', 'your StatsDeck numbers', 'StatsDeck's read "
    "on this matchup'. Don't preface routine answers by name-dropping upstream data feeds; "
    "that's plumbing noise. Keep the experience feeling like StatsDeck, not a thin wrapper.\n\n"
    "SOURCING — BE HONEST WHEN IT MATTERS: Never conceal or misrepresent where data comes "
    "from. When the user asks where the data comes from, or when attribution is materially "
    "relevant (e.g. explaining that expected stats are derived from MLB's ball-tracking), "
    "attribute accurately: game logs, season stats, probable pitchers, and injury/IL data "
    "come from the MLB Stats API; contact-quality metrics (xwOBA, barrel rate, exit velocity) "
    "are derived from MLB's Statcast tracking system and accessed via Baseball Savant. "
    "StatsDeck ANALYZES MLB's data — it does not generate the underlying tracking data, it is "
    "not affiliated with, official to, or endorsed by MLB, and StatsDeck is not itself "
    "'Statcast' (an MLB product); StatsDeck uses that data. Always keep an honest answer "
    "available for 'where does this data come from?'\n\n"
    "Always note relevant caveats (sample size, data age). When a tool response includes "
    "suggestions, surface them to guide the user's next step."
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
        icons=_SERVER_ICONS,
        website_url=_MCP_SERVER_URL or None,
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
        icons=_SERVER_ICONS,
        website_url=_MCP_SERVER_URL or None,
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
                "If the counting stats are lagging, this is a textbook buy-low — "
                "want me to scan him for buy-low value?"
            )
        elif xwoba <= 0.275:
            suggestions.append(
                f"Weak underlying contact (xwOBA {xwoba:.3f}). The surface stats may be outrunning "
                "the real quality — worth a sell-high look before you commit a roster spot."
            )

    if barrel is not None and barrel >= 0.12:
        suggestions.append(
            f"Barrel rate {barrel:.1%} is elite (top ~10% of MLB). "
            "The power's for real regardless of his recent HR count — "
            "happy to stack him up against your current option."
        )

    if hh is not None and hh >= 0.50:
        suggestions.append(
            f"Hard-hit rate {hh:.1%} — contact quality is excellent. "
            "Check his recent game log to see if the results are catching up yet."
        )

    if not suggestions:
        suggestions.append(
            "Want a gut check? I can compare him to a roster alternative, "
            "or run a buy-low / sell-high read if a trade's on the table."
        )
    return suggestions


def _suggest_recent(data: dict) -> list[str]:
    games = data.get("games_played", 0)
    if games == 0:
        return ["No games found in this window. Try a longer period or confirm the player is active."]
    suggestions: list[str] = []
    if games < 5:
        suggestions.append(
            f"Only {games} games — small sample, so don't read too much into it. "
            "Peek at his contact quality before making a roster call."
        )
    else:
        suggestions.append(
            "Want to know if this form is real? Check the contact quality — strong metrics mean "
            "it sticks; soft contact with a hot BABIP means it's about to cool off."
        )
    return suggestions


def _suggest_season_stats(data: dict) -> list[str]:
    return [
        "Season stats are the backdrop — check his contact quality to see if they're sustainable, "
        "and his recent log to see whether he's hot or cold right now."
    ]


def _suggest_pitchers(data: dict) -> list[str]:
    count = data.get("game_count", 0)
    suggestions = [
        "Want the week's best streamers? I can rank the available starters by matchup, "
        "ballpark, and recent form."
    ]
    if count == 0:
        suggestions.insert(0, "No games found for this date — try an adjacent date or check if it's an off day.")
    return suggestions


def _suggest_bvp(data: dict) -> list[str]:
    matchup = data.get("matchup", {})
    pas = matchup.get("plate_appearances", 0)
    if pas < 10:
        return [
            f"Only {pas} PAs in this matchup this season — too small to lean on. "
            "Trust his contact quality and recent form more than this for tonight's call."
        ]
    return [
        "Solid matchup sample. Factor in the ballpark tonight to round out the start/sit picture."
    ]


def _suggest_injuries(data: dict) -> list[str]:
    if data.get("on_injured_list"):
        return [
            "He's on the IL. Want me to size up your waiver options for a replacement that fits your categories?"
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
            "Bump up your hitters here, and steer your streamers away from this one — "
            "I can find better spots if you need them."
        ]
    if rf <= 93:
        return [
            f"{stadium} suppresses offense (run factor {rf}). "
            "Temper your hitters' expectations — but it's a great spot to stream a pitcher. "
            "I can dig up starters with a home start here."
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
                f"(Δ xwOBA {abs(diff):.3f}). Trust that over short-term counting stats. "
                "Thinking about dealing one for the other? I can grade the trade."
            ]
    return [
        "Want the full picture? I can grade a trade between them — category fit and "
        "rest-of-season outlook included."
    ]


# ---------------------------------------------------------------------------
# Tools: league profile
# ---------------------------------------------------------------------------

@mcp.tool()
@instrument_tool
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
            "You're all set! Now we can really cook — set your lineup for the week, hunt for "
            "buy-low and sell-high guys, or find the best streamers. Everything's tuned to your "
            "league from here on out."
        ],
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@instrument_tool
def get_league_profile() -> dict:
    """
    Retrieve your stored league settings.

    Returns the league setup the user stored earlier, including scoring format,
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
                "No league set up yet. Just tell me your league — scoring, categories, and whether "
                "lineups lock daily or weekly — and I'll tailor every call to it."
            ],
        }
    return {
        "success": True,
        "source": "local profile storage",
        "data": {"profile": profile, "summary": profile_summary(profile)},
        "suggestions": [
            "Got your league loaded. Want to set your lineup for the week, hunt some buy-low guys, "
            "or find the best streamers? Everything's tuned to your setup."
        ],
    }


# ---------------------------------------------------------------------------
# Tool: how_to_use
# ---------------------------------------------------------------------------

_HELP_TOPICS: dict[str, str] = {
    "buy low": (
        "**Buy-low hunting** — finding guys whose underlying quality is way better than their results.\n\n"
        "The signal: strong contact quality (xwOBA ≥ .360 or barrel rate ≥ 10%) sitting on weak counting "
        "stats or a cold average. He's stinging the ball — the results just haven't caught up yet, and "
        "positive regression is coming.\n\n"
        "**What I'll dig into:** his contact-quality numbers, his season line (expected vs. actual), and "
        "his recent game log to see whether the slump is fresh or a longer pattern.\n\n"
        "**Just ask** — \"Find me some buy-low guys: [players]\" — and I'll run the full read on each.\n\n"
        "**One trap to dodge:** weak contact AND weak results means he's just struggling. That's not a "
        "buy-low, that's a bad week with a bad process behind it."
    ),
    "sell high": (
        "**Sell-high hunting** — spotting guys who are outrunning their underlying metrics.\n\n"
        "The tells: a sky-high BABIP (> .340 rarely lasts), a home-run spike with no barrel rate to back "
        "it (> 20% HR/FB almost never holds), or a shiny average parked on a weak xwOBA.\n\n"
        "**What I'll dig into:** his contact quality, his BABIP versus his career norm, and whether the "
        "hot streak is recent and already starting to cool.\n\n"
        "**Just ask** — \"Who should I sell high? [players]\" — and I'll flag who's genuinely good versus "
        "who's just running hot.\n\n"
        "**Timing is everything.** Sell while his name value is at its peak — before the regression shows "
        "up in the box score and everyone else catches on."
    ),
    "streaming": (
        "**Streaming pitchers** — grabbing a free-agent starter for a week, then moving on.\n\n"
        "**What makes a good streamer:**\n"
        "1. A soft opponent (weak offense)\n"
        "2. A pitcher-friendly ballpark (run factor < 96)\n"
        "3. Two starts in the week — that's double the value\n"
        "4. A respectable baseline (ERA < 4.50, WHIP < 1.35)\n"
        "5. Good recent form\n\n"
        "**Daily vs. weekly lineups change everything.** In daily leagues you can stream aggressively and "
        "chase each day's best matchup. In weekly leagues you're locked in, so you need more confidence "
        "before you pull the trigger.\n\n"
        "**Just ask** — \"Who are the best streamers this week?\" — and I'll surface the top arms with "
        "every factor already weighed."
    ),
    "trades": (
        "**Grading trades** — never judge one in a vacuum.\n\n"
        "**How I think about it:**\n"
        "1. **Current value** — who's producing more per game right now?\n"
        "2. **Sustainable quality** — whose contact metrics actually back up the production?\n"
        "3. **Category fit** — does it patch one of your weak spots, or punch a hole in a strength?\n"
        "4. **Health** — always check the injury picture before you say yes\n\n"
        "**The classic mistake:** trusting season stats over underlying quality. A .290 hitter with a "
        ".330 xwOBA is worth more than a .305 hitter with a .270 xwOBA — the second guy's results are "
        "living on borrowed time.\n\n"
        "**Just ask** — \"Grade this trade: I give up [players], I get [players]\" — for a straight verdict "
        "on value, rest-of-season, and category fit.\n\n"
        "Tell me your league setup first if you haven't — trade advice gets a lot sharper once I know "
        "your categories."
    ),
    "lineup": (
        "**Setting your lineup** — start/sit calls and streaming.\n\n"
        "**How I rank the decision (most important first):**\n"
        "1. **Health** — injury status first; never start a guy who's about to hit the IL\n"
        "2. **Matchup** — pitcher quality and ballpark beat a short hot or cold streak\n"
        "3. **Current form** — streaks are real, but they don't override an ugly matchup\n"
        "4. **Underlying quality** — contact metrics are the tiebreaker on the close calls\n\n"
        "**For pitchers:** a two-start week beats a one-start week every time when you're streaming — so "
        "I'll scan the whole week's probables first.\n\n"
        "**Just ask** — \"Set my lineup for the week: [roster]\" — and I'll work through form, matchups, "
        "and ballparks to give you a clean start/sit board."
    ),
    "waivers": (
        "**Working the waiver wire** — ranking pickups by what they're actually worth.\n\n"
        "**The order I prioritize:**\n"
        "1. Healthy (not on the IL or day-to-day)\n"
        "2. Hot recent form that's backed by real contact quality\n"
        "3. A favorable schedule over the next couple weeks\n"
        "4. Positional need and category fit\n\n"
        "**Just ask** — \"Size up my waiver options: [players]\" — for a ranked list with the reasoning "
        "spelled out.\n\n"
        "**FAAB tip:** bid up on the guys with strong underlying metrics and temporary bad luck. That's "
        "exactly where you beat the managers who only stare at the box score."
    ),
}

_GENERAL_HELP = """**StatsDeck — your fantasy baseball edge**

I turn live MLB data into start/sit, trade, and waiver calls, with an eye on what the box score \
misses: contact quality and regression — the difference between a hot streak that's real and one \
that's about to crash. Here's how to get rolling:

**First, tell me your league** — scoring type, the categories you play, and whether lineups lock \
daily or weekly. I'll tailor everything to it, and you won't have to repeat yourself.

**And if you want team-specific calls, paste your roster.** Copy it straight from your league app \
— however it's laid out, bench and positions and all, no cleanup needed — and I'll turn generic \
lookups into real start/sit and add/drop decisions for *your* team. Toss in any waiver or \
free-agent guys you're eyeing and I'll stack your roster against what's available. Totally \
optional, and just for this conversation for now — paste it again next time and I'll pick right \
back up.

**What I can do for you:**
- **Set your lineup for the week** — start/sit across your roster (form + matchups + ballparks)
- **Spot buy-low and sell-high guys** — who's about to heat up, and who's about to cool off
- **Find the best streaming pitchers** — ranked by matchup, ballpark, and skill
- **Grade a trade** — current value, rest-of-season outlook, and category fit
- **Size up your waiver options** — ranked by production, quality, and schedule

**Or just ask me anything:**
1. "My roster: [list players]. Help me set my lineup for this week."
2. "I'm considering trading away [player A] for [player B] — is that a good deal?"
3. "Who should I pick up on waivers? My options are: [list players]."
4. "Is [player]'s hot streak real or a BABIP mirage?"
5. "Who are good streaming pitchers this week?"

Quick hits work too — "How's Aaron Judge been hitting lately?", "What's Freddie Freeman's contact \
quality?", "Who's pitching tonight?", "Is Mookie Betts on the IL?", "How hitter-friendly is Coors?"

Pick a play above or just name a player — let's get into it."""


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@instrument_tool
def how_to_use(topic: str = "") -> dict:
    """
    Get guidance on using StatsDeck — available workflows, example questions, and expert tips.

    Call with no arguments for a general orientation. Reach for this (no topic) when a NEW or
    UNSURE user opens vaguely — "what can you do?", "help", "getting started", "I'm new", a bare
    greeting — so you can orient them with the current workflow list instead of guessing. Do NOT
    call it when the user has asked a specific question; just answer that.
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
        else "\n\n💡 **Heads up:** I don't have your league yet. Tell me your scoring, categories, "
        "and whether lineups lock daily or weekly, and every answer gets sharper."
    )

    content = (text or _GENERAL_HELP) + profile_note

    return {
        "success": True,
        "source": "StatsDeck built-in guidance",
        "data": {"topic": topic_key or "general", "content": content},
        "suggestions": [
            "Tell me your league setup if you haven't yet — it makes every answer a lot sharper."
        ] if not profile else [],
    }


# ---------------------------------------------------------------------------
# Tool: get_player_stats  (recent form + full-season totals, one tool)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@instrument_tool
def get_player_stats(
    player_name: str,
    timeframe: str = "recent",
    days: int = 14,
    season: int | None = None,
) -> dict:
    """
    Get a player's production stats over a chosen timeframe. One tool, two reads:

    - **timeframe="recent"** (default): game-by-game stats over the last N `days` —
      the primary signal for current form and hot/cold streaks. A 10–14 day window
      captures momentum without over-indexing on one good or bad game. Use it for
      start/sit timing, checking a pitcher's last two weeks before streaming, and
      gauging trade value (hot = sell high, cold = potential buy-low).
    - **timeframe="season"**: full-season batting or pitching totals — the
      year-to-date baseline. Use it for context before a trade, for ERA/WHIP/K-rate
      before streaming a pitcher, or to compare actual results against contact
      quality (is the average propped up by a high BABIP?).

    Recent form tells you what's happening right now; season totals tell you the
    baseline. For *why* a player is producing — whether it's real or luck — pair
    this with get_player_statcast (contact quality).

    If the user has pasted their roster in this conversation, frame the read around
    THEIR team — start/sit, add/drop, or who-to-target — instead of just reciting
    the line. If this player is on their roster, make the call for them.

    Args:
        player_name: Full player name (e.g. "Shohei Ohtani", "Spencer Strider")
        timeframe: "recent" for game-by-game form (default), or "season" for full-season totals
        days: Lookback window when timeframe="recent" (default 14; 7 for short-term heat,
              30 for a broader trend). Ignored when timeframe="season".
        season: Season year when timeframe="season" (defaults to current season; use prior
                years for historical context). Ignored when timeframe="recent".

    Returns:
        timeframe="recent": per-game stat lines with date, opponent, home/away.
        timeframe="season": standard counting + rate stats — batters get AVG/OBP/SLG/OPS
        plus HR, RBI, SB, R; pitchers get ERA/WHIP/K9. Source: MLB Stats API.
    """
    tf = (timeframe or "recent").strip().lower()

    if tf in ("recent", "form", "recent_games", "last", "games"):
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

    if tf in ("season", "year", "full", "season_stats", "total", "totals"):
        return _wrap(mlb_stats.get_player_season_stats, player_name, season,
                     suggester=_suggest_season_stats)

    return _err(
        f"Unknown timeframe '{timeframe}'. Use 'recent' for game-by-game form "
        "or 'season' for full-season totals."
    )


# ---------------------------------------------------------------------------
# Tool: get_player_statcast
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@instrument_tool
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

    If the user has pasted their roster in this conversation, turn the read into a
    decision for THEIR team — buy-low/hold if this player is theirs and the contact
    quality says regression is coming, or a who-to-target call if he's a free agent
    they're eyeing — not just a metrics dump.

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
@instrument_tool
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

    If the user has pasted their roster in this conversation, map these matchups
    onto THEIR hitters and pitchers — flag the start/sit and streaming calls for
    their actual team rather than just listing the day's arms.

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
@instrument_tool
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
    get_player_stats (recent form) more heavily.

    If the user has pasted their roster in this conversation, turn this into a
    start/sit call for THEIR hitter against tonight's arm rather than a neutral
    matchup readout.

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
@instrument_tool
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

    If the user has pasted their roster in this conversation, tie the IL news back
    to THEIR team — who needs replacing and a fill-in to target — rather than just
    reporting the status.

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
@instrument_tool
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

    If the user has pasted their roster in this conversation, apply the park read to
    THEIR players and targets — a start/sit nudge or a who-to-target lean — instead
    of a standalone park rating.

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
@instrument_tool
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

    If the user has pasted their roster in this conversation, land the comparison on
    a decision for THEIR team — start A over B, drop one for the other, or which to
    target off waivers — not just a neutral side-by-side.

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
# Internal helper: resolve_player_name (NOT a tool — no permission prompt)
#
# Name disambiguation already happens inside every data tool via
# player_resolver.require_player(). This wrapper stays available as an internal
# helper that returns the structured {success, data, ...} shape, but it is no
# longer registered as a user-facing MCP tool.
# ---------------------------------------------------------------------------

def resolve_player_name(player_name: str) -> dict:
    """
    Resolve a player name to their MLBAM and FanGraphs IDs (internal helper).

    Disambiguates players with the same last name and handles common
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
@instrument_prompt
def getting_started() -> str:
    """
    Start here. The "I just connected — what now?" onboarding entry point.

    Orients a new user: what StatsDeck does, the guided workflows available, and
    example questions — and nudges them to set their league profile if they haven't.
    This is the canonical orientation to use whenever a user opens vaguely or signals
    they're new ("what can you do?", "help", "I'm new", a bare greeting). Reserve it
    for those open-ended openers — when the user asks a specific question, answer that
    directly instead. Takes no arguments.
    """
    profile = get_current_profile()
    if profile:
        p_sum = profile_summary(profile)
        profile_block = (
            f"Their league profile is already set — **{p_sum}**. Mention that every workflow "
            "below is tuned to their scoring and categories, so they can dive straight in."
        )
    else:
        profile_block = (
            "Their league profile is NOT set yet. Warmly nudge them to set it up first: ask for "
            "scoring type (roto / points / categories), the categories they play, and whether "
            "lineups lock daily or weekly. Tell them every workflow gets sharper once it's stored, "
            "and that they can just say something like "
            "*\"Set up my league: 12-team roto 5x5, daily lineups\"* (or describe their own)."
        )

    return f"""Greet me as StatsDeck and get me oriented. Keep it warm, confident, and concise — \
this is my first time here.

{profile_block}

Then introduce StatsDeck and how to start, roughly like this and in StatsDeck's own voice:

**StatsDeck — your fantasy baseball edge.** StatsDeck turns live MLB data into start/sit, \
trade, and waiver calls, with a focus on what the box score misses: contact quality and \
regression — the difference between a real hot streak and one about to crash.

**Here's what I can do for you — pick one and jump in:**
- **Set your lineup for the week** — start/sit across your roster (form + matchups + ballparks)
- **Spot buy-low and sell-high guys** — who's about to heat up, and who's about to cool off
- **Find the best streaming pitchers** — ranked by matchup, ballpark, and skill
- **Grade a trade** — judge a deal against your categories and roster needs
- **Size up your waiver options** — ranked by production, quality, and schedule

**Want team-specific calls? Paste your roster.** Copy it straight from your league app — \
however it's laid out, bench and positions and all, no cleanup needed — and I'll give you real \
start/sit and add/drop decisions instead of generic player lookups. Drop in any waiver or \
free-agent guys you're eyeing too, and I'll stack your roster up against what's out there. \
Totally optional — I'm great for one-off player questions either way; the roster just unlocks \
the "what should I actually do" layer.

**Or just ask naturally:**
- "My roster: [players]. Set my lineup for this week."
- "Is [player]'s hot streak real or about to crash?"
- "Should I trade [player A] for [player B]?"
- "Who should I stream this week?"

Extend that roster invitation warmly and keep it low-pressure — invite, don't nag. Mention \
lightly that for now the roster rides along with this conversation, so a quick "paste it again \
next time and I'll pick right back up" is all the explanation it needs. Then close by asking \
which workflow I'd like to start with, or invite me to paste my roster.

Stay in StatsDeck's voice — lead with StatsDeck's analysis and don't preface anything with \
upstream data-feed names. If I ask where the data comes from, answer honestly (MLB Stats API \
for stats/schedules/injuries; contact-quality metrics derived from MLB's Statcast tracking via \
Baseball Savant), and note StatsDeck analyzes MLB's data and isn't affiliated with MLB."""


@mcp.prompt()
@instrument_prompt
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
Call `get_player_stats(player_name, timeframe="recent", days=10)` for every player listed. Flag anyone with fewer than 3 hits over the past 10 days (cold) or 5+ multi-hit/multi-RBI games (hot).

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
@instrument_prompt
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
2. `get_player_stats(player_name, timeframe="season")` — actual results to compare against expected quality
3. `get_player_stats(player_name, timeframe="recent", days=14)` — is the slump recent or a longer pattern?

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
@instrument_prompt
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
2. `get_player_stats(player_name, timeframe="season")` — full season context, especially BABIP and HR/FB rate
3. `get_player_stats(player_name, timeframe="recent", days=14)` — is this a recent hot streak or sustained performance?

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
@instrument_prompt
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
- `get_player_stats(pitcher_name, timeframe="season")` — confirm ERA < 4.50, WHIP < 1.35 as baseline
- `get_player_stats(pitcher_name, timeframe="recent", days=14)` — flag anyone on a recent bad run (e.g., 2+ starts with 4+ ER)

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
@instrument_prompt
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
1. `get_player_stats(player_name, timeframe="season")` — year-to-date production and baseline
2. `get_player_stats(player_name, timeframe="recent", days=14)` — current form going into the trade
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
@instrument_prompt
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

1. `get_player_stats(player_name, timeframe="recent", days=14)` — recent form (highest weight)
2. `get_player_statcast(player_name, days=21)` — is the form backed by real contact quality?
3. `get_player_stats(player_name, timeframe="season")` — full season context (not just a hot week)
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
        path = request.url.path
        if path in ("/health", "/healthz"):
            return Response(
                content='{"status":"ok"}',
                status_code=200,
                media_type="application/json",
            )
        # Icon / favicon are public so Claude can fetch the connector logo unauthenticated.
        if path in _PUBLIC_PATHS:
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if not (auth.startswith("Bearer ") and auth[7:].strip() == self._token):
            return Response(
                content='{"error":"Unauthorized — provide Authorization: Bearer <MCP_AUTH_TOKEN>"}',
                status_code=401,
                media_type="application/json",
            )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Static assets (StatsDeck logo) — auth-exempt
# ---------------------------------------------------------------------------

async def _serve_icon(request: Request) -> Response:
    """
    Serve the StatsDeck logo. Backs both the MCP server icon (advertised in
    serverInfo as _ICON_URL) and the root favicon fallback. No auth required.
    """
    if not os.path.exists(_ICON_FILE):
        logger.warning("Icon requested but file missing at %s", _ICON_FILE)
        return Response(status_code=404)
    return FileResponse(
        _ICON_FILE,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


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

    # Serve the logo (connector icon src + favicon fallback) in every auth mode.
    for _icon_path in ("/static/icon.png", "/favicon.ico", "/favicon.png"):
        app.routes.append(Route(_icon_path, _serve_icon, methods=["GET"]))
    logger.info("Serving StatsDeck icon at %s (file=%s exists=%s)",
                _ICON_URL, _ICON_FILE, os.path.exists(_ICON_FILE))

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
