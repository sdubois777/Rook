"""Kickers and defenses must actually get recommended (#464).

Reported from a live ESPN mock draft: once every other roster slot was full, the
engine never recommended a kicker or a defense. It recommended another skill
player and reported "All starter needs filled", so a roster finished the draft
with its K and DEF slots EMPTY — scoring zero at both.

Three compounding causes, each covered below:

  1. THE CANDIDATE POOL never contained them. _get_top_available returns the top
     20 undrafted by OVERALL adp_rank; kickers and defenses rank ~460+, so while
     any better-ranked skill player was undrafted — hundreds always are — not one
     reached the cut.
  2. THE REACH GUARD would have vetoed them anyway. It refuses a need pick more
     than NEED_RANK_WINDOW (75) ranks below the board's best; a kicker is ~260
     below by construction, so the veto was unconditional.
  3. THE MESSAGE was false. "All starter needs filled" was printed whenever no
     CANDIDATE was available, not when no NEED remained.

The round guardrail (no K/DEF before the last KDEF_FINAL_ROUNDS rounds) is
unchanged and re-asserted here, because opening the window wider would be its own
bug.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from backend.engines.dependency_resolver import DependencyResolver
from backend.engines.draft_state_manager import DraftStateManager, LeagueConfig
from backend.engines.live_draft import (
    KDEF_FINAL_ROUNDS,
    NEED_RANK_WINDOW,
    LiveDraftEngine,
)
from backend.engines.opponent_threat import OpponentThreatAnalyzer


def _player(name, pos, rank):
    p = MagicMock()
    p.name = name
    p.position = pos
    p.team_abbr = "ATL"
    p.adp_rank = rank
    p.adp_fantasypros = Decimal(str(rank))
    p.adp_diff = Decimal("0")
    p.snake_flag = None
    p.tier = 1
    p.injury_status = None
    return p


def _engine(pool):
    """A snake engine whose player table is `pool` (ordered by rank, as the query
    returns it)."""
    state = DraftStateManager(LeagueConfig(draft_type="snake", team_count=12), "You")
    result = MagicMock()
    result.scalars.return_value.all.return_value = pool
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    eng = LiveDraftEngine(
        state=state, resolver=DependencyResolver(),
        threat_analyzer=OpponentThreatAnalyzer(),
        db_session_factory=MagicMock(return_value=ctx),
        ws_manager=MagicMock(broadcast=AsyncMock()),
    )
    return eng, state


def _realistic_pool():
    """What the board actually looks like: a deep run of skill players, then
    kickers and defenses far below them."""
    pool = [_player(f"Skill {i}", "WR" if i % 2 else "RB", i + 1) for i in range(400)]
    pool += [_player(f"Kicker {i}", "K", 460 + i) for i in range(5)]
    pool += [_player(f"Defense {i}", "DEF", 470 + i) for i in range(5)]
    return pool


def _fill_every_slot_except(state, skip=("K", "DEF")):
    """Fill each starter slot the league actually has, except `skip`, plus bench."""
    slots = state.league_config.roster_slots
    plan = []
    for pos, n in slots.items():
        if pos in skip:
            continue
        for _ in range(int(n or 0)):
            plan.append({"FLEX": "RB", "SUPER_FLEX": "QB", "BENCH": "WR"}.get(pos, pos))
    for i, pos in enumerate(plan):
        state.record_snake_pick(
            player_name=f"Mine {i}", position=pos,
            pick_number=i + 1, round_num=i + 1, is_yours=True,
        )


# ---------------------------------------------------------------------------
# 1. The candidate pool
# ---------------------------------------------------------------------------
def test_pool_surfaces_kdef_despite_ranking_far_below_the_cut():
    """THE ROOT CAUSE. The top 20 by rank are all skill players; the kicker and
    defense the roster actually needs sit at rank 460+ and must still appear."""
    eng, state = _engine(_realistic_pool())
    _fill_every_slot_except(state)

    pool = asyncio.run(eng._get_top_available())
    positions = {p["position"] for p in pool}
    assert "K" in positions, "kicker never reachable — this is the reported bug"
    assert "DEF" in positions, "defense never reachable — this is the reported bug"

    # Only the BEST one at each need position is added, not the whole tail.
    assert [p["name"] for p in pool if p["position"] == "K"] == ["Kicker 0"]
    assert [p["name"] for p in pool if p["position"] == "DEF"] == ["Defense 0"]

    # Element 0 must remain the board's true best available — the pick logic
    # reads it as BPA, so the extras have to be appended, never prepended.
    assert pool[0]["adp_rank"] == 1


def test_pool_does_not_pad_positions_the_roster_does_not_need():
    """A roster with K and DEF already filled must not have them re-added."""
    eng, state = _engine(_realistic_pool())
    _fill_every_slot_except(state, skip=())        # everything filled
    pool = asyncio.run(eng._get_top_available())
    assert {p["position"] for p in pool} & {"K", "DEF"} == set()


def test_pool_is_still_capped_when_needs_are_already_represented():
    """No behaviour change in the ordinary case: an empty roster needs skill
    positions, which the top 20 already cover."""
    eng, _ = _engine(_realistic_pool())
    pool = asyncio.run(eng._get_top_available())
    assert len(pool) <= 20 + 2


def test_pool_covers_a_skill_need_that_falls_below_the_cut():
    """Not a K/DEF special case. A league still needing a TE in a late round,
    when the top 20 are all RB and WR, had the same blind spot."""
    pool = [_player(f"Skill {i}", "WR" if i % 2 else "RB", i + 1) for i in range(300)]
    pool.append(_player("Deep TE", "TE", 305))
    eng, state = _engine(pool)
    _fill_every_slot_except(state, skip=("TE", "K", "DEF"))

    names = [p["name"] for p in asyncio.run(eng._get_top_available())]
    assert "Deep TE" in names


# ---------------------------------------------------------------------------
# 2. The pick decision
# ---------------------------------------------------------------------------
def test_recommends_the_kicker_when_it_is_the_only_open_slot():
    """The reported scenario, end to end."""
    eng, state = _engine(_realistic_pool())
    _fill_every_slot_except(state, skip=("K",))    # only K open
    total = state.league_config.total_roster_size

    available = asyncio.run(eng._get_top_available())
    pick, why, need = eng._deterministic_your_turn_pick(available, total)

    assert pick["position"] == "K", f"recommended {pick['position']} instead: {why}"
    assert need == "high"


def test_recommends_the_defense_when_it_is_the_only_open_slot():
    eng, state = _engine(_realistic_pool())
    _fill_every_slot_except(state, skip=("DEF",))
    total = state.league_config.total_roster_size

    available = asyncio.run(eng._get_top_available())
    pick, _, _ = eng._deterministic_your_turn_pick(available, total)
    assert pick["position"] == "DEF"


def test_reach_guard_no_longer_vetoes_a_kicker():
    """A kicker is ~260 ranks below the board's best, far outside the 75-rank
    window. The guard asks a question that has no meaning for a position where
    every player ranks that low and the slot cannot be filled any other way."""
    eng, state = _engine(_realistic_pool())
    _fill_every_slot_except(state)
    total = state.league_config.total_roster_size

    available = asyncio.run(eng._get_top_available())
    bpa_rank = available[0]["adp_rank"]
    kicker = next(p for p in available if p["position"] == "K")
    assert kicker["adp_rank"] - bpa_rank > NEED_RANK_WINDOW   # the guard would fire

    pick, why, _ = eng._deterministic_your_turn_pick(available, total)
    assert pick["position"] in ("K", "DEF")
    assert "too far a reach" not in why


def test_reach_guard_still_protects_skill_positions():
    """No regression: the guard must still refuse an absurd reach for a real
    player, which is the case it exists for."""
    pool = [_player("Great WR", "WR", 1)]
    pool += [_player(f"Filler WR {i}", "WR", 10 + i) for i in range(30)]
    pool.append(_player("Distant TE", "TE", 400))
    eng, state = _engine(pool)
    _fill_every_slot_except(state, skip=("TE", "K", "DEF"))

    available = asyncio.run(eng._get_top_available())
    pick, why, _ = eng._deterministic_your_turn_pick(available, 8)
    assert pick["name"] == "Great WR"
    assert "too far a reach" in why


# ---------------------------------------------------------------------------
# 3. The reasoning text
# ---------------------------------------------------------------------------
def test_does_not_claim_starters_are_filled_when_they_are_not():
    """The message the user actually saw. It contradicted their own roster."""
    eng, state = _engine([_player(f"Skill {i}", "WR", i + 1) for i in range(30)])
    _fill_every_slot_except(state)                 # K and DEF still open
    total = state.league_config.total_roster_size

    available = asyncio.run(eng._get_top_available())   # no K/DEF exist in this pool
    _, why, _ = eng._deterministic_your_turn_pick(available, total)

    assert "All starter needs filled" not in why
    assert "DEF" in why and "K" in why                 # names what is still open


def test_still_says_starters_are_filled_when_they_genuinely_are():
    eng, state = _engine([_player(f"Skill {i}", "WR", i + 1) for i in range(30)])
    _fill_every_slot_except(state, skip=())            # everything filled
    total = state.league_config.total_roster_size

    available = asyncio.run(eng._get_top_available())
    _, why, need = eng._deterministic_your_turn_pick(available, total)
    assert "All starter needs filled" in why
    assert need == "low"


# ---------------------------------------------------------------------------
# 4. The round guardrail is unchanged
# ---------------------------------------------------------------------------
def test_still_refuses_kdef_before_the_final_rounds():
    """Opening the window earlier would be its own bug — a kicker in round 3
    wastes a pick that cannot be recovered."""
    eng, state = _engine(_realistic_pool())
    _fill_every_slot_except(state)
    total = state.league_config.total_roster_size

    available = asyncio.run(eng._get_top_available())
    too_early = total - KDEF_FINAL_ROUNDS          # last round before the window
    pick, _, _ = eng._deterministic_your_turn_pick(available, too_early)
    assert pick["position"] not in ("K", "DEF")
