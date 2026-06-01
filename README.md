# MLB Fantasy MCP Server

An MCP (Model Context Protocol) server that gives Claude live MLB data for fantasy baseball analysis. Chat with Claude to get player stats, Statcast metrics, probable pitchers, injuries, and head-to-head matchups.

## Quick start

```bash
git clone <repo>
cd mlb-fantasy-mcp
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

> **Note:** pybaseball downloads a player lookup table (~2 MB) on first use. This is cached automatically.

### Run locally (stdio — for Claude Desktop)

```bash
python server/main.py
```

### Run as HTTP server (for remote deploy)

```bash
MCP_TRANSPORT=http MCP_PORT=8000 python server/main.py
```

## Claude Desktop integration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "mlb-fantasy": {
      "command": "/absolute/path/to/mlb-fantasy-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/mlb-fantasy-mcp/server/main.py"],
      "env": {
        "MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

**WSL users:** use the WSL path (`/home/innov/mlb-fantasy-mcp/...`) or configure via Windows path if your Claude Desktop runs on Windows. See the WSL section below.

### WSL + Claude Desktop (Windows) setup

Claude Desktop on Windows can't call WSL paths directly. Options:
1. **Copy to Windows filesystem:** `cp -r /home/innov/mlb-fantasy-mcp /mnt/c/Users/innov/mlb-fantasy-mcp` — then point Claude Desktop at `C:\Users\innov\mlb-fantasy-mcp`
2. **Run HTTP transport in WSL, connect Claude Desktop to localhost:** set `MCP_TRANSPORT=http` in WSL, then add as an HTTP MCP server in Claude Desktop at `http://localhost:8000`

## Tools

| Tool | Source | Description |
|---|---|---|
| `get_player_season_stats` | MLB Stats API | Full season batting/pitching stats |
| `get_player_recent` | MLB Stats API | Per-game log for last N days |
| `get_player_statcast` | Baseball Savant | xwOBA, barrel rate, exit velo, hard-hit% |
| `get_probable_pitchers` | MLB Stats API | Today's (or any date's) starters |
| `get_batter_vs_pitcher` | Baseball Savant | Head-to-head Statcast matchup |
| `get_injuries` | MLB Stats API | Team or player IL status |
| `get_park_factors` | Baseball Savant | Run/HR park factors vs league average |
| `compare_players` | Both | Side-by-side two-player comparison |
| `resolve_player_name` | pybaseball | Disambiguate player names → IDs |

## Data sources

**MLB Stats API** (`statsapi.mlb.com`) — free, no auth. Used for season stats, game logs, probable pitchers, IL status. Cached 15 min – 1 hour.

**Baseball Savant** (via pybaseball) — free scraper. **Only source for Statcast** (barrel rate, xwOBA, exit velocity, hard-hit%). Cached 3 hours to respect rate limits.

> **Statcast note:** Baseball Savant is the sole provider of Statcast data. The MLB Stats API does not expose these metrics. If the Savant scraper breaks after a site update, Statcast tools will return a clear error — not stale data.

## Cache strategy

| Data type | Default TTL | Rationale |
|---|---|---|
| Player ID lookups | 7 days | IDs are permanent |
| Season stats | 1 hour | Updated after each game |
| Recent game logs | 15 min | In-game or just-finished |
| Statcast / Savant | 3 hours | Rate-limit buffer |
| Probable pitchers | 30 min | Updated through game day |
| Injuries | 10 min | Can change during game |
| Park factors | 24 hours | Near-static |

All TTLs are configurable via environment variables (see `.env.example`). Cache is stored in `cache/data/` (excluded from git). Clear it with `rm -rf cache/data/`.

**Future:** swap `cache/__init__.py` backend for Redis by replacing `diskcache.Cache` with a Redis client — the interface (`get`, `set`, `make_key`) stays the same.

## Environment variables

Copy `.env.example` to `.env` and source it, or set in your shell:

```bash
MCP_TRANSPORT=stdio          # or "http"
MCP_HOST=0.0.0.0             # HTTP only
MCP_PORT=8000                # HTTP only
CACHE_TTL_STATCAST=10800     # seconds
CACHE_TTL_SEASON_STATS=3600
CACHE_TTL_RECENT=900
CACHE_TTL_PLAYER_ID=604800
CACHE_TTL_PITCHERS=1800
CACHE_TTL_INJURIES=600
```

## Running tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Tests hit real APIs — they require internet access and take ~30s due to pybaseball downloads.

## Known limitations (beta)

- **Park factors** are 2024 approximations; live Savant endpoint is planned.
- **Batter vs. pitcher** requires both players to have appeared in games this season for data; small sample sizes are expected early in the year.
- **IL data** includes minor-league 7-day IL entries since the API exposes full org rosters.
- **Rate limiting:** Statcast pulls via pybaseball can be throttled by Baseball Savant (~1 req/3s internally). The 3-hour cache is the mitigation.

## Paid-product roadmap

When moving to a paid product, these are the touch points (nothing in the beta needs to be rewritten):

1. **Auth / user accounts:** Add OAuth middleware in front of `server/main.py`. FastMCP's HTTP transport can sit behind any ASGI middleware.
2. **Per-user caching:** Replace `cache/__init__.py` with Redis + user-keyed namespaces.
3. **Tiered tools:** Gate `get_player_statcast` and `compare_players` behind a paid tier at the tool-call level in `server/main.py`.
4. **Premium data sources:** Each `sources/` module has a single fetch function per data type. Swap in a paid feed (e.g. SportsDataIO, Stats Perform) by replacing the fetch call — the tool layer is unchanged.
5. **Projections (Steamer/ZiPS):** Add `sources/projections.py` following the same pattern.
6. **Yahoo/ESPN roster sync:** Add `sources/fantasy_platform.py` + new tools.
7. **Real-time in-game data:** Add WebSocket source module; rest of stack unchanged.
8. **Billing:** Wire Stripe webhooks to update a user's tier in your auth layer.
