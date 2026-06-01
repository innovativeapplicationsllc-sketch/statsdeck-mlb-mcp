"""
League profile storage — keyed by user_id.

Beta: one JSON file per user under profile_data/.
Multi-tenant upgrade: implement a new class satisfying ProfileStorage and
assign it to _storage. The user_id gets injected from OAuth context; no
callers change.
"""

import json
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

DEFAULT_USER = "default"

# ---------------------------------------------------------------------------
# Storage interface — swap without touching callers
# ---------------------------------------------------------------------------

@runtime_checkable
class ProfileStorage(Protocol):
    def get(self, user_id: str) -> dict | None: ...
    def save(self, user_id: str, profile: dict) -> None: ...


class JsonFileStorage:
    """Beta: one JSON file per user, stored under profile_data/."""

    def __init__(self, data_dir: Path | None = None):
        self._dir = data_dir or Path(__file__).parent.parent / "profile_data"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, user_id: str) -> Path:
        safe = "".join(c for c in user_id if c.isalnum() or c in "-_")[:64] or "default"
        return self._dir / f"{safe}.json"

    def get(self, user_id: str) -> dict | None:
        p = self._path(user_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except Exception as exc:
            logger.warning("Failed to read profile %s: %s", user_id, exc)
            return None

    def save(self, user_id: str, profile: dict) -> None:
        try:
            self._path(user_id).write_text(
                json.dumps(profile, indent=2, ensure_ascii=False)
            )
        except Exception as exc:
            logger.error("Failed to save profile %s: %s", user_id, exc)
            raise


# Module-level singleton. To go multi-tenant: replace with a DB-backed class.
_storage: ProfileStorage = JsonFileStorage()


def get_profile(user_id: str = DEFAULT_USER) -> dict | None:
    return _storage.get(user_id)


def save_profile(user_id: str = DEFAULT_USER, profile: dict | None = None) -> None:
    _storage.save(user_id, profile or {})


# ---------------------------------------------------------------------------
# Helpers used in prompts
# ---------------------------------------------------------------------------

def profile_summary(profile: dict | None) -> str:
    """One-liner suitable for embedding in prompt text."""
    if not profile:
        return (
            "No league profile set — call set_league_profile() for personalized advice. "
            "Defaulting to standard 5x5 roto assumptions."
        )
    parts: list[str] = []
    st = profile.get("scoring_type", "")
    if st:
        parts.append(st.replace("_", " "))
    cats = profile.get("categories") or {}
    h = cats.get("hitting", [])
    p = cats.get("pitching", [])
    if h:
        parts.append(f"hitting: {'/'.join(h)}")
    if p:
        parts.append(f"pitching: {'/'.join(p)}")
    lock = profile.get("lineup_lock", "")
    if lock:
        parts.append(f"{lock} lineups")
    style = profile.get("league_style", "")
    if style:
        parts.append(style)
    size = profile.get("league_size")
    if size:
        parts.append(f"{size} teams")
    return ", ".join(parts) if parts else "profile set (no detail)"


def key_hitting_cats(profile: dict | None) -> list[str]:
    """Return hitting categories from profile, or sensible defaults."""
    if not profile:
        return ["R", "HR", "RBI", "SB", "AVG"]
    return (profile.get("categories") or {}).get("hitting", ["R", "HR", "RBI", "SB", "AVG"])


def key_pitching_cats(profile: dict | None) -> list[str]:
    if not profile:
        return ["W", "SV", "K", "ERA", "WHIP"]
    return (profile.get("categories") or {}).get("pitching", ["W", "SV", "K", "ERA", "WHIP"])


def is_daily_lineup(profile: dict | None) -> bool:
    return (profile or {}).get("lineup_lock", "daily") == "daily"


def is_dynasty(profile: dict | None) -> bool:
    return (profile or {}).get("league_style", "redraft") in ("dynasty", "keeper")
