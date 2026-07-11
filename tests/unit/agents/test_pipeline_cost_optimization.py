"""
Pipeline cost optimization — prompt caching + value-delta staleness.

Covers the two levers of the $10.82/run fix:
  1. PROMPT CACHING: build_system_blocks puts a 1h-TTL cache breakpoint on the
     system prefix (the 5-min default expires mid-run — the known trap), and
     shared_context rides in the cached prefix.
  2. STALENESS = VALUE-DELTA, NOT TOUCH-TIME: a bulk rewrite with unchanged
     values keeps the fingerprint identical (dirty ~0); a real injury flip
     changes exactly that player's fingerprint (dirty 1).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.agents.base_agent import build_system_blocks
from backend.agents.player_profiles import (
    PLAYER_PROFILES_PROMPT_VERSION,
    compute_input_fingerprint,
    profile_needs_refresh,
)


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Prompt caching — system block construction
# ---------------------------------------------------------------------------

def test_system_blocks_carry_1h_ttl_cache_breakpoint():
    """The LAST block carries cache_control with ttl='1h' — NOT the 5-minute
    default, which expires mid-run (~40 min) and silently re-bills full price."""
    blocks = build_system_blocks("SYSTEM PROMPT")
    assert blocks == [{
        "type": "text", "text": "SYSTEM PROMPT",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }]


def test_shared_context_joins_cached_prefix_breakpoint_on_last():
    """shared_context becomes a second system block; the breakpoint moves to it
    (prefix caching covers system + shared context together)."""
    blocks = build_system_blocks("SYSTEM", "TEAM CONTEXT")
    assert len(blocks) == 2
    assert "cache_control" not in blocks[0]
    assert blocks[1]["text"] == "TEAM CONTEXT"
    assert blocks[1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


# ---------------------------------------------------------------------------
# Fingerprint — value-delta semantics
# ---------------------------------------------------------------------------

def _player(injury_status=None, team="LAC", depth=1, contract_year=False):
    return SimpleNamespace(
        injury_status=injury_status, team_abbr=team,
        depth_chart_order=depth, contract_year=contract_year,
    )


def _team_system(qb="Justin Herbert", grade="A"):
    return SimpleNamespace(
        system_grade=grade, qb_name=qb, qb_tier="elite", rookie_qb_flag=False,
        compound_risk_flag=False, oc_scheme="west_coast",
        red_zone_philosophy="pass_heavy",
        # decoys that must NOT enter the fingerprint:
        updated_at=_now(), id="row-id-1", notes="regenerated prose",
    )


def test_bulk_rewrite_with_unchanged_values_keeps_fingerprint():
    """The bulk-sync bug: rewriting rows (new timestamps, new row ids) with
    UNCHANGED values must hash identically → dirties ~0 players."""
    a = compute_input_fingerprint(_player("Q"), team_system=_team_system())
    b = compute_input_fingerprint(_player("Q"), team_system=_team_system())
    assert a == b  # fresh objects, new timestamps/ids — same values, same hash


def test_injury_flip_changes_fingerprint():
    healthy = compute_input_fingerprint(_player(None))
    questionable = compute_input_fingerprint(_player("Q"))
    assert healthy != questionable


def test_team_and_depth_changes_change_fingerprint():
    base = compute_input_fingerprint(_player())
    assert compute_input_fingerprint(_player(team="NYJ")) != base
    assert compute_input_fingerprint(_player(depth=2)) != base


def test_dependency_row_ids_do_not_affect_fingerprint():
    """roster_changes deletes+reinserts flags — identical VALUES with new row
    ids must not dirty the player."""
    def dep(row_id):
        return SimpleNamespace(
            id=row_id, updated_at=_now(),  # decoys
            flag_type="CONTINGENT", trigger_player_name="Keenan Allen",
            trigger_condition="active_and_healthy", effect_on_value="negative",
            value_impact_pct="-0.35", confidence="high", season_year=2026,
        )
    a = compute_input_fingerprint(_player(), dependencies=[dep("id-1")])
    b = compute_input_fingerprint(_player(), dependencies=[dep("id-2")])
    assert a == b


def test_new_beat_signal_changes_fingerprint():
    a = compute_input_fingerprint(_player(), beat_signal_ids=["s1"])
    b = compute_input_fingerprint(_player(), beat_signal_ids=["s1", "s2"])
    assert a != b


def test_team_system_prose_regen_does_not_dirty():
    """team_notes rewrites prose each run — NOT one of the 7 material fields the
    profile prompt consumes, so it must not dirty every player on the team."""
    ts1 = _team_system(); ts1.notes = "one prose rendering"
    ts2 = _team_system(); ts2.notes = "a different prose rendering"
    assert (
        compute_input_fingerprint(_player(), team_system=ts1)
        == compute_input_fingerprint(_player(), team_system=ts2)
    )
    ts3 = _team_system(qb="Backup Guy")  # a MATERIAL field change does dirty
    assert (
        compute_input_fingerprint(_player(), team_system=ts1)
        != compute_input_fingerprint(_player(), team_system=ts3)
    )


# ---------------------------------------------------------------------------
# profile_needs_refresh — fingerprint path wins; legacy fallback preserved
# ---------------------------------------------------------------------------

def test_fingerprint_match_overrides_noisy_timestamps():
    """The heavy-news-day scenario: a bulk sync stamped injury_updated_at NEWER
    than the profile, but the VALUES didn't change → fingerprints match → NOT
    stale (the old timestamp logic would have said stale)."""
    fp = "same-fingerprint"
    assert profile_needs_refresh(
        profile_updated_at=_now() - timedelta(days=5),
        injury_updated_at=_now(),                # bulk-stamped, value unchanged
        dep_updated_at=_now(),                   # ditto
        stored_prompt_version=PLAYER_PROFILES_PROMPT_VERSION,
        stored_fingerprint=fp, current_fingerprint=fp,
    ) is False


def test_fingerprint_mismatch_dirties():
    assert profile_needs_refresh(
        profile_updated_at=_now() - timedelta(days=1),
        stored_prompt_version=PLAYER_PROFILES_PROMPT_VERSION,
        stored_fingerprint="old", current_fingerprint="new",
    ) is True


def test_missing_stored_fingerprint_falls_back_to_timestamps():
    """Pre-fingerprint profiles keep the legacy behavior until their next write."""
    assert profile_needs_refresh(
        profile_updated_at=_now() - timedelta(days=5),
        injury_updated_at=_now(),                # newer than profile → stale
        stored_prompt_version=PLAYER_PROFILES_PROMPT_VERSION,
        stored_fingerprint=None,
        current_fingerprint="computed",
    ) is True


def test_prompt_version_and_age_still_trump_fingerprint():
    fp = "same"
    assert profile_needs_refresh(          # version bump wins
        profile_updated_at=_now() - timedelta(days=1),
        stored_prompt_version="v-ancient",
        stored_fingerprint=fp, current_fingerprint=fp,
    ) is True
    assert profile_needs_refresh(          # 30-day safety net wins
        profile_updated_at=_now() - timedelta(days=45),
        stored_prompt_version=PLAYER_PROFILES_PROMPT_VERSION,
        stored_fingerprint=fp, current_fingerprint=fp,
    ) is True
