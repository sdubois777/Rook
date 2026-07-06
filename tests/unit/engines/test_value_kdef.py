"""K/DEF static streaming valuation (T1 of #3).

K and DEF take a SEPARATE path — no profile, no PAR, no usage engine. value_kdef()
writes the shared output fields directly so the draft/trade surfaces read them with
zero position awareness.
"""
from decimal import Decimal
from types import SimpleNamespace

from backend.engines.valuation import (
    DRAFTABLE_POSITIONS,
    MAX_REALISTIC_BID,
    value_kdef,
)


def _p(position, team="SF"):
    # A bare object with NO `profile` — proves value_kdef never touches the
    # skill projection→PAR→value chain (K/DEF have no clean_season_baseline).
    return SimpleNamespace(position=position, team_abbr=team, name=f"{team} {position}")


def test_kdef_stay_out_of_the_skill_draftable_set():
    assert "K" not in DRAFTABLE_POSITIONS
    assert "DEF" not in DRAFTABLE_POSITIONS


def test_defense_static_fields():
    p = _p("DEF", "SF")
    value_kdef(p)
    assert p.tier == 5                                    # streamer floor tier
    assert p.baseline_value == Decimal("1")
    assert p.ceiling_value == Decimal("1")
    assert p.floor_value == Decimal("1")
    assert p.risk_adjusted_value == Decimal("1")
    assert p.recommended_bid_ceiling == Decimal("1")
    assert p.let_go_threshold == Decimal("1")
    assert p.ai_bid_ceiling == 1
    assert p.adp_ai == Decimal("130")                     # DEF range start
    assert p.positional_scarcity_modifier == Decimal("1.00")
    assert p.value_gap is None                            # no market comparison
    assert p.value_gap_signal == "aligned"
    assert p.data_confidence == "low"


def test_kicker_adp_and_bid():
    p = _p("K", "DAL")
    value_kdef(p)
    assert p.tier == 5
    assert p.adp_ai == Decimal("140")                     # K range start
    assert p.ai_bid_ceiling == 1
    assert p.recommended_bid_ceiling == Decimal("1")


def test_bid_never_exceeds_the_2_dollar_cap():
    # $1 base clamped to MAX_REALISTIC_BID — reuses the existing K/DEF cap ($2),
    # so it can never exceed it regardless of the base.
    for pos in ("K", "DEF"):
        p = _p(pos)
        value_kdef(p)
        assert p.recommended_bid_ceiling <= Decimal(str(MAX_REALISTIC_BID[pos]))
        assert p.ai_bid_ceiling <= MAX_REALISTIC_BID[pos]
        assert MAX_REALISTIC_BID[pos] == 2                # the cap the value clamps to


def test_value_kdef_does_not_read_a_profile():
    # No `.profile` attribute at all — must not raise (proves the K/DEF path is
    # independent of the skill projection/PAR machinery).
    p = _p("DEF")
    value_kdef(p)
    assert p.tier == 5


def test_defense_and_kicker_are_flat_within_position():
    # Static assignment → every DEF identical, every K identical (documented
    # flat ordering pending the FantasyPros hook).
    sf, phi = _p("DEF", "SF"), _p("DEF", "PHI")
    value_kdef(sf); value_kdef(phi)
    assert (sf.tier, sf.baseline_value, sf.adp_ai) == (phi.tier, phi.baseline_value, phi.adp_ai)
