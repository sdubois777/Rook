"""Phase 2 (part 1) — the DRAFT-ROOM wait/take is format-aware via PER-FORMAT ADP.

The draft board is a PRE-DRAFT surface, so its scoring-dependent signal (ADP) reads the
league-format row from player_format_values — a pass-catcher's market ADP falls LATER in
Standard, so a "take now" in PPR becomes a "wait" in Standard. PPR is byte-identical (the
overlay is skipped entirely for PPR).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from backend.engines.draft_state_manager import LeagueConfig, DraftStateManager
from backend.engines.dependency_resolver import DependencyResolver
from backend.engines.opponent_threat import OpponentThreatAnalyzer
from backend.engines.live_draft import LiveDraftEngine

YOUR_TEAM = "Stephen"


def _wr_player():
    p = MagicMock()
    p.yahoo_player_id = "nfl.p.500"
    p.id = "uuid-500"
    p.name = "Slot Machine"          # a reception-dependent WR
    p.position = "WR"
    p.team_abbr = "CIN"
    p.tier = 2
    p.baseline_value = Decimal("30")
    p.market_value = Decimal("25")
    p.ai_bid_ceiling = 30
    p.recommended_bid_ceiling = Decimal("30")
    p.availability_factor = Decimal("1.0")
    p.notes = ""
    p.pay_up_flag = False
    p.value_assessment = "good_value"
    p.injury_status = None
    p.injury_profile = None
    p.profile = None
    p.adp_ai = Decimal("14.0")         # our AI snake pick — PPR (early)
    p.adp_fantasypros = Decimal("18.0")  # PPR market ADP (near the pick)
    p.adp_scoring = "ppr"
    p.dependencies = []
    return p


def _engine(scoring_format, pfv_adp_fp):
    cfg = LeagueConfig(auction_budget=0, draft_type="snake", scoring_format=scoring_format)
    state = DraftStateManager(cfg, YOUR_TEAM)
    ws = MagicMock()
    ws.broadcast = AsyncMock()

    # ONE result object serves both queries: the player lookup reads .scalars().all(),
    # the per-format PFV lookup reads .scalar_one_or_none().
    pfv_row = SimpleNamespace(
        tier=4,
        adp_fantasypros=Decimal(str(pfv_adp_fp)) if pfv_adp_fp is not None else None,
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [_wr_player()]
    result.scalar_one_or_none.return_value = pfv_row
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)

    resp = MagicMock()
    resp.content = [MagicMock(text="{}")]
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=resp)

    eng = LiveDraftEngine(
        state=state, resolver=DependencyResolver(),
        threat_analyzer=OpponentThreatAnalyzer(),
        db_session_factory=factory, ws_manager=ws,
    )
    eng._client = client
    return eng, ws


def _advice(scoring_format, pfv_adp_fp):
    eng, ws = _engine(scoring_format, pfv_adp_fp)
    # Empty roster → WR is an open need; nominated at pick 20.
    asyncio.run(eng.on_nomination({
        "player_id": "nfl.p.500", "player_name": "Slot Machine", "current_pick": 20,
    }))
    return ws.broadcast.call_args_list[0][0][0]   # the deterministic recommendation


def test_ppr_take_now():
    """PPR: the pass-catcher's market ADP is near the pick → take him now (unchanged)."""
    msg = _advice("ppr", pfv_adp_fp=40)   # PFV ignored on the PPR path
    assert msg["action"] == "draft"
    assert msg["scoring_format"] == "ppr"


def test_standard_wait():
    """Standard: his format-matched market ADP (~40) falls well past pick 20 → he'll be
    here later, so WAIT — the same nomination flips from PPR 'take now'."""
    msg = _advice("standard", pfv_adp_fp=40)
    assert msg["action"] == "wait"
    assert "market ADP" in msg["reasoning"]
    assert msg["adp_fp"] == 40.0          # format-matched market ADP surfaced
    assert msg["adp_scoring"] == "standard"


def test_standard_populated_performat_adp_is_not_defaulted():
    """With a per-format market ADP populated, the record is format-matched (not a PPR
    fallback) — adp_fantasypros == the PFV value and the disclosure flag is off."""
    eng, _ = _engine("standard", pfv_adp_fp=40)
    rec = asyncio.run(eng._get_player_record("nfl.p.500"))
    assert rec["adp_fantasypros"] == 40.0
    assert rec["adp_format_defaulted"] is False
    assert rec["scoring_format"] == "standard"


def test_standard_missing_performat_adp_discloses_ppr_fallback():
    """No per-format market ADP → keep the players-table PPR ADP AND flag the fallback so
    the UI can disclose 'showing PPR ADP'. The wait/take override does NOT fire on it."""
    eng, ws = _engine("standard", pfv_adp_fp=None)
    rec = asyncio.run(eng._get_player_record("nfl.p.500"))
    assert rec["adp_fantasypros"] == 18.0            # players-table PPR value
    assert rec["adp_format_defaulted"] is True
    # And the decision does NOT flip off a PPR-fallback ADP — it stays "take now".
    msg = _advice("standard", pfv_adp_fp=None)
    assert msg["action"] == "draft"
