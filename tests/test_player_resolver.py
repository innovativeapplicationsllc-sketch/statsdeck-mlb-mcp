"""
Regression tests for the player name resolver.

Anchored by the "Michael King" bug: he's registered in the player database as
"Mike King", so a naive (last, first) fuzzy lookup for "Michael King" returned
the 5 most similar *other Michaels* (Michael Tonkin, etc.) and the resolver
picked one of them. The fix: anchor on the last name, score the full same-last
cohort with nickname awareness, and refuse to guess when not confident.

Two layers:
  * TestResolverUnit — pure scoring/normalization helpers, no network (fast).
  * TestResolverLive — end-to-end resolution via pybaseball (hits the network).

Run: .venv/bin/python -m pytest tests/test_player_resolver.py -v
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from sources import player_resolver as pr


# ---------------------------------------------------------------------------
# Offline unit tests — the logic that decides matches, no network.
# ---------------------------------------------------------------------------

class TestResolverUnit:
    def test_nicknames_michael_mike(self):
        assert pr._are_nicknames("michael", "mike")
        assert pr._are_nicknames("mike", "michael")
        # The crux of the bug: "michael"→"mike" must score as a perfect match,
        # even though raw string similarity ranks "hal"/"charles" higher.
        assert pr._first_name_score("michael", "mike") == 1.0
        assert pr._first_name_score("michael", "hal") < 1.0
        assert pr._first_name_score("michael", "charles") < 1.0
        assert pr._first_name_score("michael", "mike") > pr._first_name_score("michael", "hal")

    def test_other_common_nicknames(self):
        for a, b in [
            ("matthew", "matt"), ("alexander", "alex"), ("nicholas", "nick"),
            ("joseph", "joe"), ("william", "billy"), ("robert", "bobby"),
            ("zachary", "zac"), ("anthony", "tony"), ("samuel", "sam"),
        ]:
            assert pr._first_name_score(a, b) == 1.0, f"{a}/{b} should be a nickname match"

    def test_exact_first_name(self):
        assert pr._first_name_score("aaron", "aaron") == 1.0

    def test_no_first_name_is_neutral(self):
        assert pr._first_name_score("", "anything") == 0.5

    def test_accent_folding(self):
        assert pr._strip_accents("Acuña") == "Acuna"
        assert pr._strip_accents("José Ramírez") == "Jose Ramirez"
        assert pr._similarity("acuna", "acuña") == 1.0

    def test_norm_last_strips_suffix_and_accents(self):
        assert pr._norm_last("Acuña Jr.") == "acuna"
        assert pr._norm_last("Witt Jr.") == "witt"
        assert pr._norm_last("King") == "king"

    def test_parse_name_variants(self):
        assert pr._parse_name("Michael King") == ("King", "Michael")
        assert pr._parse_name("King, Michael") == ("King", "Michael")
        last, first = pr._parse_name("Ronald Acuna Jr.")
        assert first == "Ronald" and "Acuna" in last

    def test_safe_int_tolerates_junk(self):
        assert pr._safe_int("") is None
        assert pr._safe_int("2019.0") == 2019
        assert pr._safe_int(None) is None
        assert pr._safe_int(660271) == 660271


# ---------------------------------------------------------------------------
# Live end-to-end tests — real resolution through pybaseball.
# ---------------------------------------------------------------------------

# Clear any in-process cache so each run resolves fresh (avoids a poisoned entry
# masking a regression).
@pytest.fixture(autouse=True)
def _clear_cache():
    import cache
    if hasattr(cache._backend, "_store"):
        cache._backend._store.clear()
    yield


class TestResolverLive:
    def test_michael_king_resolves_to_padres_king(self):
        """THE BUG: 'Michael King' must resolve to Mike King (mlbam 650633),
        never to Michael Tonkin / Michael Pérez / any other Michael."""
        r = pr.resolve_player("Michael King")
        assert r is not None, "Michael King should resolve, not return None"
        assert r["player"]["mlbam_id"] == 650633
        assert r["player"]["name_display"].lower().endswith("king")
        # And specifically NOT the wrong players from the original bug report.
        assert "tonkin" not in r["player"]["name_display"].lower()
        assert "pérez" not in r["player"]["name_display"].lower()
        assert "perez" not in r["player"]["name_display"].lower()

    def test_mike_king_also_resolves(self):
        r = pr.resolve_player("Mike King")
        assert r is not None and r["player"]["mlbam_id"] == 650633

    def test_require_player_michael_king(self):
        assert pr.require_player("Michael King")["mlbam_id"] == 650633

    @pytest.mark.parametrize("name,mlbam", [
        ("Aaron Judge", 592450),
        ("Shohei Ohtani", 660271),
        ("Freddie Freeman", 518692),
        ("Mookie Betts", 605141),
        ("Gerrit Cole", 543037),
        ("Juan Soto", 665742),
        ("Corbin Burnes", 669203),
    ])
    def test_known_players_still_resolve(self, name, mlbam):
        """Don't break players that already resolved correctly."""
        r = pr.resolve_player(name)
        assert r is not None, f"{name} should resolve"
        assert r["player"]["mlbam_id"] == mlbam

    @pytest.mark.parametrize("name,mlbam", [
        ("Ronald Acuna Jr.", 660670),   # accent + suffix
        ("Ronald Acuña Jr.", 660670),   # with the ñ
        ("Yordan Alvarez", 670541),     # accent
        ("Bobby Witt Jr.", 677951),     # suffix; father "Bobby Witt" also exists
    ])
    def test_accented_and_suffixed_names(self, name, mlbam):
        r = pr.resolve_player(name)
        assert r is not None and r["player"]["mlbam_id"] == mlbam

    def test_shared_exact_name_picks_active_and_flags_ambiguous(self):
        """Multiple 'Jose Ramirez' exist — pick the active star (608070) via the
        recency tiebreaker, and flag ambiguous so alternatives can be surfaced."""
        r = pr.resolve_player("Jose Ramirez")
        assert r is not None
        assert r["player"]["mlbam_id"] == 608070
        assert r["ambiguous"] is True
        assert len(r["alternatives"]) >= 1

    @pytest.mark.parametrize("garbage", [
        "Asdfgh Qwerty",
        "Zzzzz Fakeplayer99",
        "Michael Tonkinx Kingg",
        "",
        "   ",
    ])
    def test_garbage_returns_none_not_wrong_player(self, garbage):
        """Returning the wrong player is worse than failing — these must be None."""
        assert pr.resolve_player(garbage) is None

    def test_require_player_raises_on_garbage(self):
        with pytest.raises(ValueError):
            pr.require_player("Asdfgh Qwerty")
