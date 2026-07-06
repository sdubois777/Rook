"""T3 per-league roster-slot config: canonical normalizer, guard, and the three
platform adapters — verified against the REAL captures/fixtures (not reasoning)."""
import re
from pathlib import Path

import pytest

from backend.services.roster_slots import (
    FLEX_ELIGIBLE,
    normalize,
    slots_from_espn,
    slots_from_sleeper,
    slots_from_yahoo,
)

_EXT = Path(__file__).resolve().parents[3] / "extension" / "test" / "fixtures"


# ---- canonical model + guard ----------------------------------------------

def test_flex_eligibility_constants():
    assert FLEX_ELIGIBLE["FLEX"] == ("RB", "WR", "TE")
    assert FLEX_ELIGIBLE["SUPER_FLEX"] == ("QB", "RB", "WR", "TE")


def test_normalize_hand_written():
    got = normalize(["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN"], platform="sleeper")
    assert got == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1, "BENCH": 1}


def test_guard_unrecognized_token_returns_none():
    # A never-seen token → whole-league fallback (None), never a partial parse.
    assert normalize(["QB", "WAT", "RB"], platform="espn", league="x") is None


def test_guard_idp_folds_to_unsupported_not_fallback():
    # Known-but-unmodeled (IDP) → UNSUPPORTED bucket; offense slots survive.
    got = normalize(["QB", "IDP_FLEX", "RB"], platform="sleeper")
    assert got == {"QB": 1, "UNSUPPORTED": 1, "RB": 1}


def test_guard_unknown_platform_returns_none():
    assert normalize(["QB"], platform="nintendo") is None


# ---- SLEEPER adapter (real captured draft frame) --------------------------

def test_sleeper_adapter_real_frame_with_derived_bench():
    # Live-confirmed auction/snake draft-frame settings. Bench is DERIVED:
    # rounds(15) − Σ starters(10) = 5.
    frame = {"slots_qb": 1, "slots_rb": 2, "slots_wr": 2, "slots_te": 1,
             "slots_flex": 2, "slots_k": 1, "slots_def": 1, "rounds": 15}
    assert slots_from_sleeper(frame) == {
        "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "K": 1, "DEF": 1, "BENCH": 5,
    }


def test_sleeper_super_flex_key_maps():
    # PRESUMED slots_super_flex → SUPER_FLEX (no real superflex capture exists).
    # rounds(4) == Σ starters(4) → no bench key added.
    frame = {"slots_qb": 1, "slots_super_flex": 1, "slots_rb": 2, "rounds": 4}
    assert slots_from_sleeper(frame) == {"QB": 1, "SUPER_FLEX": 1, "RB": 2}


# ---- ESPN adapter (parse the REAL fixtures) -------------------------------

@pytest.mark.parametrize("fmt", ["salarycap", "snake"])
def test_espn_adapter_from_real_fixture(fmt):
    html = (_EXT / "espn" / fmt / "board-mid.html").read_text(encoding="utf-8")
    # The resolver's stable anchor: div[title="Position"] — first team's 16 slots.
    tokens = re.findall(r'title="Position"[^>]*>([^<]{1,6})', html)[:16]
    assert tokens[:9] == ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "D/ST", "K"]
    assert slots_from_espn(tokens) == {
        "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "DEF": 1, "K": 1, "BENCH": 7,
    }


def test_espn_op_superflex_maps():
    assert slots_from_espn(["QB", "OP", "RB"])["SUPER_FLEX"] == 1


# ---- YAHOO adapter (parse the REAL pre-draft capture, CONCATENATED) -------

def _yahoo_tokens(html: str) -> list[str]:
    """Extract each YOUR-TEAM badge's CONCATENATED span letters (the flex badge is
    <span>W</span><span>R</span><span>T</span> → 'WRT'; one span misreads 'W')."""
    badges = re.findall(
        r'W\(32px\) H\(32px\)[^"]*"[^>]*>((?:<[^>]*>)*?(?:<span[^>]*>[A-Z/]{1,5}</span>)+)', html
    )
    return ["".join(re.findall(r'<span[^>]*>([A-Z/]{1,5})</span>', b)) for b in badges]


def test_yahoo_adapter_from_real_predraft_capture():
    html = (_EXT / "auction" / "lobby.html").read_text(encoding="utf-8")  # n/15 = 0, empty roster
    tokens = _yahoo_tokens(html)
    assert "WRT" in tokens  # flex correctly concatenated, not a phantom "W"
    assert slots_from_yahoo(tokens, total_check=15) == {
        "QB": 1, "WR": 2, "RB": 2, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1, "BENCH": 6,
    }


def test_yahoo_phantom_w_would_corrupt_wr_count():
    # Proof the concatenation matters: the single-span (wrong) read makes the flex
    # a phantom 'W' → normalize can't map 'W' → guard fires (safe), NOT a silent
    # WR-count corruption.
    assert slots_from_yahoo(["QB", "WR", "WR", "RB", "RB", "TE", "W", "K", "DEF"], total_check=15) is None


def test_yahoo_total_checksum_mismatch_falls_back():
    assert slots_from_yahoo(["QB", "WR", "WR"], total_check=15) is None


def test_yahoo_qwrt_superflex_maps():
    assert slots_from_yahoo(["QB", "QWRT"])["SUPER_FLEX"] == 1


# ---- roster-needs payoff + config fallback (the behavioral change) --------

from types import SimpleNamespace  # noqa: E402

from backend.engines.draft_state_manager import (  # noqa: E402
    DraftStateManager,
    LeagueConfig,
    _DEFAULT_ROSTER_SLOTS,
)


def test_roster_needs_no_def_league_never_wants_def():
    cfg = LeagueConfig(roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "K": 1, "BENCH": 5})
    needs = DraftStateManager(cfg).format_roster_needs([])
    assert "DEF" not in needs
    assert needs.count("FLEX: 1 more") == 2  # two flex slots


def test_roster_needs_superflex_wants_qb_eligible_slot():
    cfg = LeagueConfig(roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "SUPER_FLEX": 1, "K": 1, "DEF": 1, "BENCH": 6})
    needs = DraftStateManager(cfg).format_roster_needs([])
    assert "SUPER_FLEX: 1 more (QB/RB/WR/TE)" in needs


def test_roster_needs_default_unchanged():
    # The standard-lineup path (#204/#205/#206) is byte-unchanged.
    needs = DraftStateManager(LeagueConfig()).format_roster_needs([])
    for tok in ("QB: need 1", "RB: need 2", "WR: need 2", "TE: need 1", "K: need 1", "DEF: need 1", "FLEX: 1 more (RB/WR/TE)"):
        assert tok in needs


def test_config_null_roster_slots_falls_back_to_default():
    lg = SimpleNamespace(budget=200, draft_type="auction", team_count=12, scoring="ppr", roster_slots=None)
    assert DraftStateManager.config_from_user_league(lg).roster_slots == _DEFAULT_ROSTER_SLOTS
    assert DraftStateManager.config_from_user_league(None).roster_slots == _DEFAULT_ROSTER_SLOTS


def test_config_real_roster_slots_used():
    lg = SimpleNamespace(budget=200, draft_type="snake", team_count=12, scoring="ppr",
                         roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "K": 1, "DEF": 1, "BENCH": 5})
    cfg = DraftStateManager.config_from_user_league(lg)
    assert cfg.roster_slots["FLEX"] == 2 and cfg.total_roster_size == 15


# ---- trade-lineup consumer (DEFAULT_LINEUP_RULES) -------------------------

from backend.services.trade.lineup import (  # noqa: E402
    DEFAULT_LINEUP_RULES,
    lineup_rules_from_slots,
)


def test_lineup_rules_null_slots_uses_demo_default():
    assert lineup_rules_from_slots(None) is DEFAULT_LINEUP_RULES


def test_lineup_rules_from_real_slots_offense_only():
    r = lineup_rules_from_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "K": 1, "DEF": 1, "BENCH": 5})
    assert r.slots == {"QB": 1, "RB": 2, "WR": 2, "TE": 1}  # no K/DEF (trade ignores them)
    assert r.flex_count == 2
    assert r.flex_positions == ("RB", "WR", "TE")


def test_lineup_rules_superflex_admits_qb():
    r = lineup_rules_from_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "SUPER_FLEX": 1})
    assert r.flex_count == 2 and r.flex_positions == ("QB", "RB", "WR", "TE")
