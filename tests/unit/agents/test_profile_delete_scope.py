"""The delete scope for player_profiles writes.

THE INVARIANT: never delete a profile we are not about to rewrite.

`_write_profiles` reads `stale_names=None` as "wipe every profile on this team", which is
right on a force / first run because everyone is about to be re-inserted. A TARGETED
refresh also set `stale_names = None` — to bypass the staleness gate — but it filters the
context down to the affected set, so the writer wiped the team and rewrote only the
targeted names.

Measured in production before this guard existed: refreshing 41 players across 6 teams
took `player_profiles` from 829 rows to 767, and the collaterally-cleared players kept
stale `ai_bid_ceiling` values that the budget enforcement then funded. Asking for one
player on SEA destroyed ~30 SEA profiles.
"""
from __future__ import annotations

from backend.agents.player_profiles import profile_delete_scope


def _ctx(*names):
    return [{"name": n} for n in names]


# --- force / first run: the full-team wipe is correct -----------------------------

def test_force_run_keeps_the_full_team_wipe():
    """stale_names None + no targeting = force run: every player is about to be
    rewritten, so wiping the team first is right."""
    assert profile_delete_scope(None, None, _ctx("A", "B", "C")) is None


def test_incremental_run_passes_the_staleness_set_through():
    """A normal incremental run deletes exactly the stale players it rewrites."""
    stale = {"A", "B"}
    assert profile_delete_scope(stale, None, _ctx("A", "B", "C")) == stale


# --- targeted refresh: the regression --------------------------------------------

def test_targeted_refresh_never_returns_the_full_team_wipe():
    """THE BUG. Targeted mode nulls stale_names to skip the gate; that must not reach the
    writer as 'delete everything'."""
    scope = profile_delete_scope(None, {"Star"}, _ctx("Star"))
    assert scope is not None
    assert scope == {"Star"}


def test_targeted_scope_is_exactly_what_gets_rewritten():
    """The context has already been filtered to the affected set, so the delete scope is
    that filtered list — not the request, and not the team."""
    # Asked for three, but only two of them are on this team with usable data.
    scope = profile_delete_scope(None, {"Star", "Other", "NotOnThisTeam"},
                                 _ctx("Star", "Other"))
    assert scope == {"Star", "Other"}


def test_targeted_refresh_of_one_player_spares_his_teammates():
    """The exact production failure: one player requested, ~30 teammates destroyed."""
    teammates = _ctx("Star")          # context filtered down to the single target
    scope = profile_delete_scope(None, {"Star"}, teammates)
    for teammate in ("Backup", "Depth1", "Depth2"):
        assert teammate not in scope


def test_targeted_refresh_with_no_survivors_deletes_nothing():
    """A targeted player with no context data must not take the team down with him."""
    assert profile_delete_scope(None, {"Ghost"}, _ctx()) == set()


def test_targeting_wins_even_when_a_staleness_set_is_present():
    """Belt and braces: if both are somehow set, the targeted filter is authoritative,
    because it is the context filter that decides what gets rewritten."""
    scope = profile_delete_scope({"A", "B", "C"}, {"A"}, _ctx("A"))
    assert scope == {"A"}
