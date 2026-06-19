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


# --- drafted-name tracking (engine excludes drafted from recommendations) ----

def test_record_snake_pick_tracks_name():
    s = DraftStateManager(_snake_config(), YOUR_TEAM)
    s.record_snake_pick("Jahmyr Gibbs")
    assert s.is_drafted("Jahmyr Gibbs") is True


def test_is_drafted_normalized_match():
    # Abbreviated DOM name matches the full pool name via first-initial+last.
    s = DraftStateManager(_snake_config(), YOUR_TEAM)
    s.record_snake_pick("Jahmyr Gibbs")
    assert s.is_drafted("J. Gibbs") is True       # abbreviated -> full
    s.record_snake_pick("C. McCaffrey")
    assert s.is_drafted("Christian McCaffrey") is True  # full -> abbreviated


def test_is_drafted_false_for_undrafted():
    s = DraftStateManager(_snake_config(), YOUR_TEAM)
    s.record_snake_pick("Jahmyr Gibbs")
    assert s.is_drafted("Bijan Robinson") is False
    assert s.is_drafted("J. Cook") is False  # same initial, different last name


def test_record_snake_pick_ignores_empty():
    s = DraftStateManager(_snake_config(), YOUR_TEAM)
    s.record_snake_pick("")
    s.record_snake_pick(None)
    assert s.is_drafted("") is False


# --- your-roster tracking (is_yours) for snake recommendations ---------------

def test_record_snake_pick_tracks_yours():
    s = DraftStateManager(_snake_config(), YOUR_TEAM)
    s.record_snake_pick("Bijan Robinson", position="RB", pick_number=1, round_num=1, is_yours=True)
    roster = s.get_my_roster()
    assert len(roster) == 1
    assert roster[0] == {
        "player_name": "Bijan Robinson", "position": "RB", "pick_number": 1, "round": 1,
    }


def test_record_snake_pick_ignores_others():
    # An opponent's pick (is_yours=False) is excluded from recs but NOT my roster.
    s = DraftStateManager(_snake_config(), YOUR_TEAM)
    s.record_snake_pick("Jahmyr Gibbs", position="RB", is_yours=False)
    assert s.get_my_roster() == []
    assert s.is_drafted("Jahmyr Gibbs") is True  # still excluded from recs


def test_get_my_roster_returns_your_picks():
    s = DraftStateManager(_snake_config(), YOUR_TEAM)
    s.record_snake_pick("Bijan Robinson", position="RB", is_yours=True)
    s.record_snake_pick("CeeDee Lamb", position="WR", is_yours=True)
    assert [p["player_name"] for p in s.get_my_roster()] == ["Bijan Robinson", "CeeDee Lamb"]
    # Returns a copy — mutating it doesn't corrupt internal state.
    s.get_my_roster().append({"player_name": "X"})
    assert len(s.get_my_roster()) == 2


def test_format_roster_needs_empty():
    s = DraftStateManager(_snake_config(), YOUR_TEAM)
    needs = s.format_roster_needs([])
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF", "FLEX"):
        assert pos in needs


def test_format_roster_needs_partial():
    s = DraftStateManager(_snake_config(), YOUR_TEAM)
    roster = [
        {"position": "RB"}, {"position": "RB"},  # RB filled (2)
        {"position": "QB"},                       # QB filled
    ]
    needs = s.format_roster_needs(roster)
    assert "QB" not in needs       # filled
    assert "RB: need" not in needs  # filled
    assert "WR" in needs            # still needed
    assert "TE" in needs
    assert "FLEX" in needs


def test_format_roster_needs_all_filled():
    s = DraftStateManager(_snake_config(), YOUR_TEAM)
    roster = [
        {"position": "QB"}, {"position": "RB"}, {"position": "RB"},
        {"position": "WR"}, {"position": "WR"}, {"position": "TE"},
        {"position": "RB"},  # surplus RB fills FLEX (6 RB/WR/TE > base 5)
        {"position": "K"}, {"position": "DEF"},
    ]
    assert s.format_roster_needs(roster) == "All starters filled — draft for depth/upside"


def test_on_your_turn_prompt_has_roster():
    eng, ws, state = _your_turn_engine(YOUR_TURN_JSON, _sample_available())
    state.record_snake_pick("CeeDee Lamb", position="WR", round_num=1, is_yours=True)
    asyncio.run(eng.on_your_turn({"round": 2, "pick": 14}))
    prompt = eng._client.messages.create.await_args.kwargs["messages"][0]["content"]
    assert "YOUR ROSTER (1 picks)" in prompt
    assert "CeeDee Lamb" in prompt


def test_on_your_turn_prompt_has_needs():
    eng, ws, state = _your_turn_engine(YOUR_TURN_JSON, _sample_available())
    asyncio.run(eng.on_your_turn({"round": 1, "pick": 1}))
    prompt = eng._client.messages.create.await_args.kwargs["messages"][0]["content"]
    assert "POSITIONS STILL NEEDED" in prompt
    # Empty roster -> QB is among the needs.
    assert "QB: need" in prompt


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


# --- on_your_turn (best-available snake recommendation) ----------------------

YOUR_TURN_JSON = (
    '{"action":"draft","player_name":"Bijan Robinson","position":"RB",'
    '"reasoning":"Elite RB value at the turn.","adp_rank":1,"adp_fp":2,'
    '"adp_diff":1,"can_wait":false,"wait_until_pick":null,'
    '"confidence":"high","position_need":"high"}'
)


def _mock_available(name, pos, rank, fp, diff, flag, ypid):
    p = MagicMock()
    p.name = name
    p.position = pos
    p.team_abbr = "ATL"
    p.adp_rank = rank
    p.adp_fantasypros = Decimal(str(fp)) if fp is not None else None
    p.adp_diff = Decimal(str(diff)) if diff is not None else None
    p.snake_flag = flag
    p.tier = 1
    p.yahoo_player_id = ypid
    return p


def _your_turn_engine(sonnet_text, available):
    state = DraftStateManager(_snake_config(), YOUR_TEAM)
    ws = MagicMock()
    ws.broadcast = AsyncMock()

    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = available
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
    return eng, ws, state


def _sample_available():
    return [
        _mock_available("Bijan Robinson", "RB", 1, 2, 1, "TARGET", "nfl.p.100"),
        _mock_available("Jahmyr Gibbs", "RB", 2, 1, -1, "TARGET", "nfl.p.101"),
    ]


def test_on_your_turn_builds_recommendation():
    eng, ws, _ = _your_turn_engine(YOUR_TURN_JSON, _sample_available())
    asyncio.run(eng.on_your_turn({"round": 1, "pick": 1}))
    msg = ws.broadcast.call_args[0][0]
    assert msg["type"] == "recommendation"
    assert msg["action"] == "draft"
    assert msg["player_name"] == "Bijan Robinson"
    assert msg["round"] == 1 and msg["pick"] == 1
    assert "bid_ceiling" not in msg  # snake, not auction


def test_your_turn_event_triggers_snake_engine():
    eng, ws, _ = _your_turn_engine(YOUR_TURN_JSON, _sample_available())
    asyncio.run(eng.handle_event({"type": "your_turn", "round": 3, "pick": 28}))
    msg = ws.broadcast.call_args[0][0]
    assert msg["type"] == "recommendation"
    assert msg["pick"] == 28


def test_snake_rec_includes_can_wait():
    eng, ws, _ = _your_turn_engine(YOUR_TURN_JSON, _sample_available())
    asyncio.run(eng.on_your_turn({"round": 1, "pick": 1}))
    assert "can_wait" in ws.broadcast.call_args[0][0]


def test_snake_rec_includes_wait_until_pick():
    eng, ws, _ = _your_turn_engine(YOUR_TURN_JSON, _sample_available())
    asyncio.run(eng.on_your_turn({"round": 1, "pick": 1}))
    assert "wait_until_pick" in ws.broadcast.call_args[0][0]


def test_can_wait_inferred_from_adp_diff_when_omitted():
    # Model omits can_wait; engine infers it from a large positive adp_diff.
    j = '{"action":"draft","player_name":"X","position":"WR","reasoning":"y","confidence":"high"}'
    avail = [_mock_available("Sleeper WR", "WR", 30, 55, 25, "VALUE", "nfl.p.200")]
    eng, ws, _ = _your_turn_engine(j, avail)
    asyncio.run(eng.on_your_turn({"round": 3, "pick": 30}))
    assert ws.broadcast.call_args[0][0]["can_wait"] is True


def test_on_your_turn_no_available_waits():
    eng, ws, _ = _your_turn_engine(YOUR_TURN_JSON, [])
    asyncio.run(eng.on_your_turn({"round": 1, "pick": 1}))
    msg = ws.broadcast.call_args[0][0]
    assert msg["action"] == "wait"
    # No Sonnet call when there's nothing to recommend.
    eng._client.messages.create.assert_not_called()


def test_on_your_turn_excludes_drafted_players():
    eng, ws, state = _your_turn_engine(YOUR_TURN_JSON, _sample_available())
    # Drafted players are excluded by NAME (snake ids don't match our DB).
    state.record_snake_pick("Bijan Robinson")
    available = asyncio.run(eng._get_top_available())
    names = [p["name"] for p in available]
    assert "Bijan Robinson" not in names  # already drafted
    assert "Jahmyr Gibbs" in names


def test_on_your_turn_excludes_abbreviated_drafted_name():
    # The snake DOM sends "B. Robinson"; the pool has "Bijan Robinson".
    eng, ws, state = _your_turn_engine(YOUR_TURN_JSON, _sample_available())
    state.record_snake_pick("B. Robinson")
    names = [p["name"] for p in asyncio.run(eng._get_top_available())]
    assert "Bijan Robinson" not in names


def test_on_your_turn_recommendation_skips_drafted():
    # Full path: draft the top player, then the your-turn rec must not name it.
    eng, ws, state = _your_turn_engine(YOUR_TURN_JSON, _sample_available())
    state.record_snake_pick("Bijan Robinson")
    asyncio.run(eng.on_your_turn({"round": 1, "pick": 1}))
    # The model was given only non-drafted players; Bijan isn't in the prompt.
    prompt = eng._client.messages.create.await_args.kwargs["messages"][0]["content"]
    assert "Bijan Robinson" not in prompt
    assert "Jahmyr Gibbs" in prompt


def test_snake_pick_recorded_into_state():
    eng, ws, state = _your_turn_engine(YOUR_TURN_JSON, _sample_available())
    asyncio.run(eng.on_pick_confirmed({
        "type": "draft_pick", "player_id": "nfl.p.101", "team_id": "Bart",
        "final_price": 0, "player_name": "Jahmyr Gibbs", "position": "RB",
    }))
    assert "nfl.p.101" in state.get_drafted_player_ids()


def test_your_turn_prompt_has_value_vs_consensus():
    from backend.engines.live_draft import _SNAKE_YOUR_TURN_PROMPT
    p = _SNAKE_YOUR_TURN_PROMPT
    assert "Value vs Consensus" in p
    assert "can_wait" in p
    assert "wait_until_pick" in p
    assert "FORBIDDEN" in p


def test_on_your_turn_broadcasts_full_payload():
    # The broadcast must carry the full recommendation, never an empty dict.
    eng, ws, _ = _your_turn_engine(YOUR_TURN_JSON, _sample_available())
    asyncio.run(eng.on_your_turn({"round": 1, "pick": 1}))
    msg = ws.broadcast.call_args[0][0]
    assert msg != {}
    for key in (
        "type", "action", "player_name", "reasoning", "adp_rank", "adp_fp",
        "adp_diff", "can_wait", "wait_until_pick", "confidence", "position",
        "position_need", "round", "pick", "elapsed_ms",
    ):
        assert key in msg, f"missing {key} in broadcast payload"
    assert msg["player_name"] == "Bijan Robinson"


def test_on_your_turn_skips_broadcast_on_empty_rec():
    # If the parsed recommendation has no player_name, the guard must skip the
    # broadcast (and log) rather than push an empty card to the UI.
    eng, ws, _ = _your_turn_engine(YOUR_TURN_JSON, _sample_available())
    eng._parse_your_turn_recommendation = lambda *a, **k: {"type": "recommendation"}
    asyncio.run(eng.on_your_turn({"round": 1, "pick": 1}))
    ws.broadcast.assert_not_called()
