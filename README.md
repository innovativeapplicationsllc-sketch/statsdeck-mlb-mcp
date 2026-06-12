# StatsDeck — MLB Fantasy MCP Server

An MCP (Model Context Protocol) server that gives Claude live MLB data and embedded fantasy baseball expertise. Chat with Claude to get guided workflows — not just raw stats, but expert-framed decisions on start/sit, trades, waivers, and streaming.

## Quick start

```bash
git clone <repo>
cd mlb-fantasy-mcp
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

> **Note:** pybaseball downloads a player lookup table (~2 MB) on first use. Cached automatically.

### Run locally (stdio — for Claude Desktop)

```bash
python server/main.py
```

### Run as HTTP server (for remote deploy)

```bash
MCP_TRANSPORT=http MCP_PORT=8000 python server/main.py
```

## First time in Claude Desktop

After connecting (see setup below), start with:

```
Call set_league_profile() with my league settings:
- scoring_type: h2h_categories
- hitting_categories: R,HR,RBI,SB,AVG
- pitching_categories: W,SV,K,ERA,WHIP
- lineup_lock: daily
- league_size: 12
```

Then ask for a guided workflow:
- *"Run a weekly lineup review for my roster: [paste players]"*
- *"Find buy-low candidates from this list: [paste players]"*
- *"Should I accept this trade: giving up [A], getting [B]?"*

## Claude Desktop integration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "statsdeck": {
      "command": "/absolute/path/to/mlb-fantasy-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/mlb-fantasy-mcp/server/main.py"],
      "env": {
        "MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

**WSL users:** Claude Desktop on Windows can't resolve WSL paths directly.
- **Option A (recommended):** Use `wsl.exe` as the command: `"command": "wsl.exe"`, `"args": ["--", "/home/innov/mlb-fantasy-mcp/.venv/bin/python", "/home/innov/mlb-fantasy-mcp/server/main.py"]`
- **Option B:** Run `MCP_TRANSPORT=http python server/main.py` in WSL, then add as an HTTP MCP server at `http://localhost:8000` in Claude Desktop settings.

See `claude_desktop_config.example.json` for copy-paste configs.

---

## Guided workflow prompts

These are the highest-value entry points. Select them from the MCP prompts menu in Claude Desktop, or just ask Claude to run them.

| Prompt | Args | What it does |
|--------|------|-------------|
| `weekly_lineup_review` | `roster` (your players), optional `week_start`/`week_end` | Full start/sit analysis: current form → matchups → park factors → Statcast validation for borderline players |
| `buy_low_finder` | `players` (comma-separated list) | Finds positive regression candidates: high xwOBA/barrel rate + lagging results |
| `sell_high_finder` | `players` | Finds overperformance candidates: high results + weak xwOBA/barrel rate (BABIP-driven, unsustainable) |
| `streaming_pitchers` | `start_date`, `end_date`, `priorities` | Ranks free-agent starters by two-start potential, park factors, opponent quality |
| `trade_evaluator` | `giving_up`, `getting` | Evaluates trade on current value + sustainable quality + category fit for your league |
| `waiver_targets` | `available_players`, optional `roster_needs` | Ranks waiver adds by form, Statcast, upcoming schedule, and category fit |

Each prompt:
- Reads your league profile automatically (set once with `set_league_profile`)
- Tells Claude exactly which tools to call and in what order
- Embeds expert framing for interpreting the results
- Adapts advice to your scoring type, categories, and daily/weekly lineup lock

---

## Tools

### League profile
| Tool | Description |
|------|-------------|
| `set_league_profile` | Save your league format, categories, roster, waivers — used by every prompt |
| `get_league_profile` | Retrieve your stored settings |
| `how_to_use` | Orientation guide; call with a topic ("buy low", "streaming", "trades") for targeted tips |

### Data tools
| Tool | Source | Fantasy use case |
|------|--------|-----------------|
| `get_player_season_stats` | MLB Stats API | Baseline context before a trade; pitcher baseline before streaming |
| `get_player_recent` | MLB Stats API | Hot/cold streak detection; current form for start/sit timing |
| `get_player_statcast` | Baseball Savant | **Buy-low/sell-high tool** — xwOBA, barrel rate, hard-hit% reveal whether results are sustainable |
| `get_probable_pitchers` | MLB Stats API | Weekly schedule planning; two-start identification |
| `get_batter_vs_pitcher` | Baseball Savant | Tonight's matchup decision; platoon advantage confirmation |
| `get_injuries` | MLB Stats API | Pre-start health check; trade due diligence |
| `get_park_factors` | Baseball Savant | Streaming pitcher venue risk; hitter projection context |
| `compare_players` | Both | Start/sit tiebreaker; waiver drop decision |
| `resolve_player_name` | pybaseball | Disambiguate names, check IDs |

### Structured response shape

Every tool returns:
```json
{
  "success": true,
  "source": "Baseball Savant (Statcast)",
  "data": { "...actual result..." },
  "suggestions": ["Next-step nudge based on what the data shows"]
}
```

The `suggestions` field coaches the next move — e.g., after `get_player_statcast` returns a high barrel rate with poor results, it will suggest `buy_low_finder`.

---

## Data sources

**MLB Stats API** (`statsapi.mlb.com`) — free, no auth. Season stats, game logs, probable pitchers, IL status. Cached 15 min – 1 hour.

**Baseball Savant** (via pybaseball) — free scraper. **Only source for Statcast** (barrel rate, xwOBA, exit velocity, hard-hit%). Cached 3 hours to respect rate limits.

> Statcast note: if the Savant scraper breaks after a site update, Statcast tools return a clear error — not stale data.

---

## League profile storage

Stored in `profile_data/default.json`. The storage layer is abstracted behind a small interface (`get`/`save` keyed by `user_id`) so multi-tenant OAuth can be added without touching any tool logic:

```
profile_data/
  default.json      ← beta: one user
  user_abc123.json  ← future: per-user after OAuth
```

---

## Cache strategy

| Data type | Default TTL | Rationale |
|---|---|---|
| Player ID lookups | 7 days | IDs are permanent |
| Season stats | 1 hour | Updated after each game |
| Recent game logs | 15 min | Near-live |
| Statcast / Savant | 3 hours | Rate-limit buffer |
| Probable pitchers | 30 min | Updated through game day |
| Injuries | 10 min | Changes during games |
| Park factors | 24 hours | Near-static |

All TTLs configurable via env vars (see `.env.example`). Cache stored in `cache/data/` (git-ignored). Clear: `rm -rf cache/data/`.

**Future:** swap `cache/__init__.py` for Redis — same `get`/`set`/`make_key` interface, no callers change.

---

## Running tests

```bash
.venv/bin/python -m pytest tests/ -v
```

- `tests/test_data_layer.py` — data sources in isolation (hits real APIs, ~30s)
- `tests/test_server.py` — MCP tool responses + all prompt content checks (~3s, mostly offline)
- `tests/test_usage_analytics.py` — usage logging behavior & safety (no DB needed, ~0.5s)

---

## Usage analytics

Per-user usage logging backed by Postgres. It is **fully additive and invisible**:
every tool/prompt invocation records one event on a background thread. If the
database is unreachable or `DATABASE_URL` is unset, **tool calls still succeed and
return normally** — events are dropped and the failure is logged server-side. The
on-request cost is ~5µs (just queuing a dict); all DB I/O is off the request path.

Discovery requests (ListTools/ListPrompts) are **never** recorded — only real user
activity (`event_type` = `tool_call` or `prompt_used`).

### 1. Set the env var (Railway)

Add a Postgres database to the project (**New → Database → Postgres**), then on the
**StatsDeck service** add a reference variable:

```
DATABASE_URL = ${{Postgres.DATABASE_URL}}
```

That's the only variable to set. It's read from env, never hardcoded. Unset it to
turn analytics off completely.

### 2. Create the table (once, reproducible migration)

```bash
DATABASE_URL=postgresql://...  python -m analytics.migrate
```

Idempotent — creates `usage_events` plus indexes on `user_id`, `created_at`,
`(user_id, created_at)`, `tool_name`, and `event_type`. Schema lives in
`analytics/schema.sql`. On Railway, run it from the service shell (where
`DATABASE_URL` is already set).

### 3. Export data for analysis

`scripts/export_usage.py` writes a clean CSV and/or JSON, with optional date-range
and user filters:

```bash
python scripts/export_usage.py                                  # all → usage_export.csv
python scripts/export_usage.py --start 2026-06-01 --end 2026-06-11
python scripts/export_usage.py --user user_2abc... --format json --out alice.json
python scripts/export_usage.py --event-type tool_call --format both --out june
```

### 4. Ready-made analytics queries

`scripts/run_analytics.py` runs named queries from `scripts/analytics_queries.sql`
with aligned table output:

```bash
python scripts/run_analytics.py --list            # dau, wau, calls-per-user,
                                                  # tool-popularity, error-rate,
                                                  # error-types, cache-hit-rate, user-history
python scripts/run_analytics.py dau               # daily active users (30d)
python scripts/run_analytics.py wau               # weekly active users (12w)
python scripts/run_analytics.py calls-per-user    # total tool calls per user
python scripts/run_analytics.py tool-popularity   # most/least used tools
python scripts/run_analytics.py error-rate        # overall + per-tool error rate
python scripts/run_analytics.py user-history --user user_2abc...
python scripts/run_analytics.py all               # everything except user-history
```

Or run the SQL directly: `psql "$DATABASE_URL" -f scripts/analytics_queries.sql`.

### Privacy

Only non-sensitive data is logged: tool/prompt name, sport, the Clerk `user_id`,
and small scalar query params (player queried, timeframe, days). No free-text and
nothing more sensitive. (Disclose usage logging in the privacy policy.)

---

## Known limitations (beta)

- **Park factors** are 2024 approximations; live Savant endpoint planned for v2.
- **Batter vs. pitcher** is thin early in the season (small sample); treated as directional only.
- **IL data** includes minor-league 7-day IL entries alongside MLB 10/15/60-day placements.
- **Statcast rate limiting:** Savant throttles heavy scrapers. The 3-hour cache is the mitigation; concurrent users may see occasional empty results.

---

## Paid-product punch list for the league profile storage

Everything in the beta stores profiles as local JSON, keyed by a `user_id` that is currently always `"default"`. To go multi-tenant:

1. **Auth middleware:** Wrap `server/main.py`'s FastMCP HTTP transport in an ASGI auth layer (FastAPI lifespan, Starlette middleware). Validate the OAuth token, extract `user_id`, and make it available to tool handlers via request context or a context variable.

2. **Pass `user_id` to profile functions:** In `server/main.py`, replace `DEFAULT_USER` with the authenticated user ID in `set_league_profile` and `get_league_profile`. The `sources/profile.py` interface already accepts `user_id` — callers just need to pass it.

3. **Swap storage backend:** Implement a new class satisfying the `ProfileStorage` protocol in `sources/profile.py`:
   ```python
   class PostgresStorage:
       def get(self, user_id: str) -> dict | None: ...
       def save(self, user_id: str, profile: dict) -> None: ...
   ```
   Assign it to `_storage`. No callers change — not the tools, not the prompts.

4. **Per-user cache namespacing:** In `cache/__init__.py`, prefix cache keys with `user_id`. When using Redis, namespace keys as `{user_id}:{tool_key}`.

5. **Tier gating:** Add a decorator in `server/main.py` that checks the user's subscription tier before executing a tool. Statcast tools and the guided prompts are natural paid-tier features.

6. **Profile in prompt context:** All 6 prompts already call `get_profile(DEFAULT_USER)`. After step 2, pass the authenticated user ID here instead — the prompts are already designed for it.

Nothing in the data layer (`sources/mlb_stats.py`, `sources/savant.py`, `sources/player_resolver.py`) changes for multi-tenancy. The seam is cleanly at the profile storage and the auth layer.
