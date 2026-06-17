"""Snake-draft engine path: draft_type threading + Sonnet snake recommendation."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from backend.engines.draft_state_manager import LeagueConfig, DraftStateManager
from backend.engines.dependency_resolver import DependencyResolver
from backend.engines.opponent_threat import OpponentThreatAnalyzer
from backend.engines.live_draft import LiveDraftEngine, _SNAKE_SYSTEM_PROMPT
from backend.routers.draft import StartDraftRequest

YOUR_TEAM = "Stephen"

SNAKE_JSON = (
    '{"action":"draft","reasoning":"Elite RB value here.","adp_ai":14,'
    '"adp_fp":18,"adp_diff":4,"position_need":"high","confidence":"high","tier":1}'
)
AUCTION_JSON = '{"action":"bid_to","bid_ceiling":55,"reasoning":"x","confidence":"high"}'


def _snake_config() -> LeagueConfig:
    return LeagueConfig(auction_budget=0, draft_type="snake", scoring_format="ppr")


def _auction_config() -> LeagueConfig:
    return LeagueConfig(draft_type="auction")


# --- LeagueConfig + DraftStateManager ----------------------------------------

def test_league_config_stores_draft_type():
    assert LeagueConfig(draft_type="snake").draft_type == "snake"


def test_league_config_stores_scoring_format():
    assert LeagueConfig(scoring_format="half_ppr").scoring_format == "half_ppr"


def test_config_from_user_league_snake():
    league = SimpleNamespace(budget=200, draft_type="snake", team_count=12, scoring="ppr")
    cfg = DraftStateManager.config_from_user_league(league)
    assert cfg.draft_type == "snake"
    assert cfg.auction_budget == 0  # no budget in snake
    assert cfg.scoring_format == "ppr"


def test_draft_state_manager_is_snake():
    s = DraftStateManager(_snake_config(), YOUR_TEAM)
    assert s.is_snake is True
    assert s.is_auction is False
    assert s.draft_type == "snake"


def test_draft_state_manager_is_auction():
    s = DraftStateManager(_auction_config(), YOUR_TEAM)
    assert s.is_auction is True
    assert s.is_snake is False


# --- engine harness ----------------------------------------------------------

def _mock_player(adp_ai=14.0):
    p = MagicMock()
    p.yahoo_player_id = "nfl.p.100"
    p.id = "uuid-100"
    p.name = "Bijan Robinson"
    p.position = "RB"
    p.team_abbr = "ATL"
    p.tier = 1
    p.baseline_value = Decimal("60")
    p.market_value = Decimal("50")
    p.ai_bid_ceiling = 60
    p.recommended_bid_ceiling = Decimal("60")
    p.notes = ""
    p.pay_up_flag = False
    p.value_assessment = "good_value"
    p.injury_profile = None
    p.profile = None
    p.adp_ai = Decimal(str(adp_ai)) if adp_ai is not None else None
    p.adp_fantasypros = Decimal("18.0")
    p.adp_scoring = "ppr"
    p.dependencies = []
    return p


def _engine(config, sonnet_text):
    state = DraftStateManager(config, YOUR_TEAM)
    ws = MagicMock()
    ws.broadcast = AsyncMock()

    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = _mock_player()
    session.execute = AsyncMock(return_value=result)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)

    resp = MagicMock()
    resp.content = [MagicMock(text=sonnet_text)]
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=resp)

    eng = LiveDraftEngine(
        state=state,
        resolver=DependencyResolver(),
        threat_analyzer=OpponentThreatAnalyzer(),
        db_session_factory=factory,
        ws_manager=ws,
    )
    eng._client = client
    return eng, ws


def _broadcast_msg(ws):
    return ws.broadcast.call_args[0][0]


def test_get_player_record_includes_adp():
    eng, _ = _engine(_snake_config(), "{}")
    rec = asyncio.run(eng._get_player_record("nfl.p.100"))
    assert rec["adp_ai"] == 14.0
    assert rec["adp_fantasypros"] == 18.0
    assert rec["adp_scoring"] == "ppr"


def test_on_nomination_routes_to_snake():
    eng, ws = _engine(_snake_config(), SNAKE_JSON)
    asyncio.run(eng.on_nomination({"player_id": "nfl.p.100", "player_name": "Bijan Robinson"}))
    msg = _broadcast_msg(ws)
    assert msg["type"] == "recommendation"
    assert msg["action"] in ("draft", "wait")  # snake action, not auction
    assert "adp_ai" in msg and "bid_ceiling" not in msg


def test_on_nomination_routes_to_auction():
    eng, ws = _engine(_auction_config(), AUCTION_JSON)
    asyncio.run(eng.on_nomination({"player_id": "nfl.p.100", "player_name": "Bijan Robinson"}))
    msg = _broadcast_msg(ws)
    assert msg["action"] in ("buy", "bid_to", "block", "pass")
    assert "bid_ceiling" in msg


def test_snake_rec_has_action_field():
    eng, ws = _engine(_snake_config(), SNAKE_JSON)
    asyncio.run(eng.on_nomination({"player_id": "nfl.p.100", "player_name": "Bijan Robinson"}))
    assert "action" in _broadcast_msg(ws)


def test_snake_rec_action_is_draft_or_wait():
    eng, ws = _engine(
        _snake_config(),
        '{"action":"wait","reasoning":"Reach — wait.","confidence":"medium"}',
    )
    asyncio.run(eng.on_nomination({"player_id": "nfl.p.100", "player_name": "Bijan Robinson"}))
    assert _broadcast_msg(ws)["action"] == "wait"


def test_snake_rec_falls_back_to_wait_on_bad_json():
    eng, ws = _engine(_snake_config(), "not json at all")
    asyncio.run(eng.on_nomination({"player_id": "nfl.p.100", "player_name": "Bijan Robinson"}))
    assert _broadcast_msg(ws)["action"] == "wait"


def test_snake_prompt_includes_injury_guardrails():
    p = _SNAKE_SYSTEM_PROMPT.lower()
    assert "forbidden" in p
    assert "injury diagnoses" in p
    assert "chronic condition" in p


def test_start_draft_request_accepts_draft_type():
    assert StartDraftRequest(your_team_id="Stephen", draft_type="snake").draft_type == "snake"
    # Still optional — defaults to None.
    assert StartDraftRequest(your_team_id="X").draft_type is None
