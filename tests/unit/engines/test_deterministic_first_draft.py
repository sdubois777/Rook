"""
Deterministic-first live draft — THE ENGINE DECIDES, SONNET EXPLAINS.

Locks the build's contracts:
  * feasibility: the bid ceiling NEVER strands the roster (incl. the old
    max(1,...) end-game bug: $0 spendable -> ceiling 0 / pass, not a $1 bid)
  * need-aware snake pick (last startable TE over a 6th WR; refuse a 5th WR)
  * round-phase guardrails in CODE (no early K/DEF, QB window)
  * roster-aware auction (no stacking a filled position when slots are reserved)
  * deterministic-first broadcast + enrichment staleness guard + retries
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.engines.dependency_resolver import DependencyResolver
from backend.engines.draft_state_manager import DraftPick, DraftStateManager, LeagueConfig
from backend.engines.live_draft import (
    KDEF_FINAL_ROUNDS,
    NEED_RANK_WINDOW,
    QB_MIN_ROUND,
    LiveDraftEngine,
)
from backend.engines.opponent_threat import OpponentThreatAnalyzer


def _engine(draft_type="auction"):
    cfg = LeagueConfig(draft_type=draft_type, team_count=12)
    state = DraftStateManager(cfg, "You")
    ws = MagicMock()
    ws.broadcast = AsyncMock()
    eng = LiveDraftEngine(
        state=state, resolver=DependencyResolver(),
        threat_analyzer=OpponentThreatAnalyzer(), db_session_factory=MagicMock(),
        ws_manager=ws,
    )
    return eng, state, ws


def _fill_roster(state, n, position="WR"):
    for i in range(n):
        state.your_roster.append(DraftPick(
            player_id=f"x{i}", team_id="You", price=1,
            player_name=f"Filler {i}", position=position,
        ))


def _avail(name, pos, rank, adp_diff=0.0):
    return {"name": name, "position": pos, "adp_rank": rank,
            "adp_fp": rank + adp_diff, "adp_diff": adp_diff,
            "snake_flag": None, "tier": None}


# ---------------------------------------------------------------------------
# PART 1 — feasibility: end-game sweep, zero infeasible bids
# ---------------------------------------------------------------------------

def test_feasibility_sweep_endgame_never_strands_roster():
    """budget $1-$5 x slots 1-5: after bidding the recommended ceiling, every
    remaining slot must still be fillable at $1. The old max(1,...) floor
    recommended an infeasible $1 at $0 spendable."""
    record = {"position": "RB", "ai_bid_ceiling": 60, "availability_factor": 1.0}
    for budget in range(1, 6):
        for slots in range(1, 6):
            eng, state, _ = _engine()
            _fill_roster(state, state.league_config.total_roster_size - slots)
            state.your_budget = budget
            spendable = state.get_spendable_on_this_player()
            ceiling = eng._calculate_live_bid_ceiling(record, 0.0, spendable)
            assert ceiling <= max(0, spendable), (budget, slots, ceiling)
            if ceiling > 0:   # a real bid must leave $1 per remaining slot
                assert budget - ceiling >= (slots - 1), (budget, slots, ceiling)


def test_zero_spendable_is_pass_not_one_dollar():
    eng, state, _ = _engine()
    _fill_roster(state, state.league_config.total_roster_size - 2)  # 2 slots left
    state.your_budget = 2                                           # spendable = 0
    record = {"position": "RB", "ai_bid_ceiling": 60, "availability_factor": 1.0,
              "name": "Stud"}
    spendable = state.get_spendable_on_this_player()
    assert spendable == 0
    ceiling = eng._calculate_live_bid_ceiling(record, 0.0, spendable)
    assert ceiling == 0
    action, bid, why = eng._deterministic_auction_action(record, ceiling, spendable, 0.0, False)
    assert action == "pass" and bid == 0


# ---------------------------------------------------------------------------
# PART 2 — need-aware snake pick + guardrails
# ---------------------------------------------------------------------------

def _seed_snake(state, positions):
    for i, pos in enumerate(positions):
        state.record_snake_pick(f"Mine {i}", position=pos, pick_number=i + 1,
                                round_num=i + 1, is_yours=True)


def test_takes_last_startable_te_over_sixth_wr():
    """Recon S5: NO TE rostered; bench-only WR out-ranks the last startable TE
    by 40 — the need must win (window > 40)."""
    eng, state, _ = _engine("snake")
    _seed_snake(state, ["RB", "RB", "WR", "WR", "WR", "RB", "WR", "QB",
                        "RB", "WR", "RB", "WR"])
    available = [
        _avail("Nico Collins", "WR", 17),      # bench-only: WR2 + FLEX all filled
        _avail("Sam LaPorta", "TE", 57),       # the open TE need
        _avail("Deep RB", "RB", 151),
    ]
    pick, why, need = eng._deterministic_your_turn_pick(available, 13)
    assert pick["name"] == "Sam LaPorta"
    assert need == "high"


def test_refuses_fifth_wr_for_real_need():
    """Recon S2: 4 WR + 1 RB rostered; board top is more WRs -> the engine must
    NOT take a 5th WR when an open need exists."""
    eng, state, _ = _engine("snake")
    _seed_snake(state, ["WR", "WR", "WR", "WR", "RB"])
    available = [
        _avail("Puka Nacua", "WR", 13),
        _avail("Mid RB", "RB", 75),
        _avail("Mid TE", "TE", 80),
    ]
    pick, why, need = eng._deterministic_your_turn_pick(available, 6)
    assert pick["position"] in ("RB", "TE")     # a real need, never the 5th WR
    assert pick["name"] != "Puka Nacua"


def test_bpa_at_need_position_wins():
    """Recon S1: BPA is a WR and WR still fills a starter slot -> take BPA."""
    eng, state, _ = _engine("snake")
    _seed_snake(state, ["WR", "TE", "RB", "RB"])
    available = [
        _avail("Emeka Egbuka", "WR", 49),
        _avail("Joe Burrow", "QB", 53),
    ]
    pick, why, need = eng._deterministic_your_turn_pick(available, 5)
    assert pick["name"] == "Emeka Egbuka"


def test_absurd_reach_guard_falls_back_to_bpa():
    eng, state, _ = _engine("snake")
    _seed_snake(state, ["WR", "WR", "WR", "WR"])   # WR filled (2 + flex + 1 over)
    available = [
        _avail("Bench WR", "WR", 10),
        _avail("Distant TE", "TE", 10 + NEED_RANK_WINDOW + 1),
    ]
    pick, _, _ = eng._deterministic_your_turn_pick(available, 8)
    assert pick["name"] == "Bench WR"              # need reach too far


def test_no_kdef_before_final_rounds():
    """The guardrail Sonnet only had as prose: K/DEF never before the final
    KDEF_FINAL_ROUNDS rounds, even as BPA at a need position."""
    eng, state, _ = _engine("snake")
    total = state.league_config.total_roster_size
    available = [
        _avail("Early Kicker", "K", 5),
        _avail("Early DST", "DEF", 6),
        _avail("Normal WR", "WR", 40),
    ]
    early = total - KDEF_FINAL_ROUNDS            # last non-eligible round
    pick, _, _ = eng._deterministic_your_turn_pick(available, early)
    assert pick["position"] not in ("K", "DEF")
    late = total - KDEF_FINAL_ROUNDS + 1         # first eligible round
    pick, _, _ = eng._deterministic_your_turn_pick(available, late)
    assert pick["position"] in ("K", "DEF")      # now a need pick


def test_no_qb_before_min_round():
    eng, state, _ = _engine("snake")
    available = [
        _avail("Josh Allen", "QB", 5),
        _avail("Solid WR", "WR", 30),
    ]
    pick, _, _ = eng._deterministic_your_turn_pick(available, QB_MIN_ROUND - 1)
    assert pick["position"] != "QB"
    pick, _, _ = eng._deterministic_your_turn_pick(available, QB_MIN_ROUND)
    assert pick["position"] == "QB"              # QB is a need once allowed


# ---------------------------------------------------------------------------
# PART 3 — roster-aware auction
# ---------------------------------------------------------------------------

def test_auction_passes_on_stacked_position_when_slots_reserved():
    """The '$60 on three RBs' fix: RB starters+flex filled and every remaining
    slot reserved for open needs -> pass, whatever the value."""
    eng, state, _ = _engine()
    # 13 filled: 3 RB (RB2 + flex) + 10 others; 3 slots left = TE/K/DEF needs
    for pos in ["RB", "RB", "RB", "WR", "WR", "WR", "QB", "WR", "WR", "WR",
                "WR", "WR", "WR"]:
        state.your_roster.append(DraftPick("x", "You", 1, "F", pos))
    state.your_budget = 60
    record = {"position": "RB", "name": "RB Stud", "system_value": 90,
              "market_value": 50, "pay_up_flag": True}
    spendable = state.get_spendable_on_this_player()
    action, bid, why = eng._deterministic_auction_action(record, 55, spendable, 0.0, False)
    assert action == "pass" and bid == 0
    assert "reserved" in why


def test_auction_buys_at_open_need():
    eng, state, _ = _engine()
    state.your_budget = 60
    record = {"position": "RB", "name": "RB Stud", "system_value": 90,
              "market_value": 50, "pay_up_flag": True}
    spendable = state.get_spendable_on_this_player()
    action, bid, why = eng._deterministic_auction_action(record, 44, spendable, 0.0, False)
    assert action == "buy" and bid == 44


# ---------------------------------------------------------------------------
# PART 4 — deterministic-first broadcast + enrichment behavior
# ---------------------------------------------------------------------------

def test_enrichment_never_changes_pick_or_number():
    eng, state, ws = _engine()
    base = {"type": "recommendation", "player_name": "Stud", "action": "buy",
            "bid_ceiling": 44, "reasoning": "det", "confidence": "high"}
    eng.last_recommendation = base
    resp = MagicMock()
    resp.content = [MagicMock(text='{"reasoning": "great fit", "confidence": "high"}')]
    eng._client = MagicMock()
    eng._client.messages.create = AsyncMock(return_value=resp)
    asyncio.run(eng._broadcast_enrichment(base, "sys", "user"))
    enriched = ws.broadcast.call_args[0][0]
    assert enriched["player_name"] == "Stud"
    assert enriched["bid_ceiling"] == 44            # number locked
    assert enriched["action"] == "buy"              # action locked
    assert enriched["reasoning"] == "great fit"     # only nuance changed
    assert enriched["ai_enriched"] is True


def test_stale_enrichment_is_dropped():
    """If a newer rec superseded this one while Sonnet was thinking, the stale
    nuance must NOT clobber it."""
    eng, state, ws = _engine()
    base = {"type": "recommendation", "player_name": "Old Player", "action": "buy",
            "bid_ceiling": 10, "reasoning": "det", "confidence": "high"}
    eng.last_recommendation = {"player_name": "Newer Player"}
    resp = MagicMock()
    resp.content = [MagicMock(text='{"reasoning": "stale", "confidence": "low"}')]
    eng._client = MagicMock()
    eng._client.messages.create = AsyncMock(return_value=resp)
    asyncio.run(eng._broadcast_enrichment(base, "sys", "user"))
    ws.broadcast.assert_not_called()


def test_draft_client_has_retries():
    """Draft-day 429 resilience: the engine's client must carry SDK retries
    (the shared client is max_retries=0)."""
    eng, _, _ = _engine()
    assert getattr(eng._client, "max_retries", 0) >= 2
