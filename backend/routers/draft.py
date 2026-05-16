"""
backend/routers/draft.py

Live draft endpoints — WebSocket push and HTTP action triggers.

WebSocket: GET /ws/draft
  React clients connect here. Receives all draft events (nominations,
  bids, picks, clock warnings) pushed from the Playwright bridge.

HTTP actions (called by React UI):
  POST /draft/bid        → place bid on current nomination
  POST /draft/nominate   → nominate a player
  POST /draft/pass       → pass on current nomination
  POST /draft/connect    → start bridge connection to Yahoo draft room
  POST /draft/start      → initialize draft engine + state manager
  GET  /draft/state      → current draft state snapshot
  POST /draft/frame      → inject a frame into the engine (testing/manual)
  GET  /draft/recommendation → last AI recommendation
  GET  /draft/opponents  → opponent budgets, threats, and combo alerts
  POST /draft/end        → close draft session
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from backend.websocket.manager import ws_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/draft", tags=["draft"])

# Module-level singletons — created on POST /draft/start
_bridge = None
_engine = None
_state = None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class BidRequest(BaseModel):
    amount: int


class NominateRequest(BaseModel):
    yahoo_player_id: str
    opening_bid: int = 1


class ConnectRequest(BaseModel):
    draft_room_url: str


class StartDraftRequest(BaseModel):
    your_team_id: str
    draft_room_url: str | None = None
    league_id: str | None = None  # user_leagues.id — loads budget/team_count


class FrameRequest(BaseModel):
    frame: dict


# ---------------------------------------------------------------------------
# WebSocket endpoint — React clients connect here
# ---------------------------------------------------------------------------

@router.websocket("/ws/draft")
async def draft_websocket(websocket: WebSocket):
    """
    Push-based WebSocket for React draft clients.
    All draft events (nominations, bids, picks) are broadcast here from the bridge.
    No polling — events arrive only when Yahoo pushes WS frames.
    """
    await ws_manager.connect(websocket)
    logger.info("Draft WebSocket client connected")
    try:
        while True:
            # Keep connection alive and handle any client → server messages
            data = await websocket.receive_json()
            logger.debug("Client message: %s", data)
            # No client → server messages needed in current design
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        logger.info("Draft WebSocket client disconnected")
    except Exception as exc:
        logger.error("Draft WebSocket error: %s", exc)
        ws_manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# Bridge lifecycle
# ---------------------------------------------------------------------------

@router.post("/connect", summary="Connect Playwright bridge to Yahoo draft room")
async def connect_bridge(req: ConnectRequest):
    """
    Launch the Playwright browser and connect to the Yahoo draft room.
    Must be called before any bid/nominate/pass actions.
    """
    global _bridge
    from backend.integrations.yahoo_playwright import YahooPlaywrightBridge

    if _bridge and getattr(_bridge, "_connected", False):
        return {"status": "already_connected", "url": _bridge._draft_room_url}

    _bridge = YahooPlaywrightBridge(ws_manager)
    try:
        await _bridge.connect(req.draft_room_url)
        return {"status": "connected", "url": req.draft_room_url}
    except Exception as exc:
        logger.error("Bridge connect failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Bridge connection failed: {exc}")


# ---------------------------------------------------------------------------
# Draft action endpoints
# ---------------------------------------------------------------------------

@router.post("/bid", summary="Place a bid on the currently nominated player")
async def place_bid(req: BidRequest):
    """Submit a bid. Bridge must be connected (POST /draft/connect first)."""
    _require_bridge()
    await _bridge.place_bid(req.amount)
    return {"status": "bid_placed", "amount": req.amount}


@router.post("/nominate", summary="Nominate a player for auction")
async def nominate_player(req: NominateRequest):
    """Nominate a player. Bridge must be connected."""
    _require_bridge()
    await _bridge.nominate_player(req.yahoo_player_id, req.opening_bid)
    return {
        "status": "nominated",
        "yahoo_player_id": req.yahoo_player_id,
        "opening_bid": req.opening_bid,
    }


@router.post("/pass", summary="Pass on the current nomination")
async def pass_nomination():
    """Pass on the current nomination. Bridge must be connected."""
    _require_bridge()
    await _bridge.pass_nomination()
    return {"status": "passed"}


# ---------------------------------------------------------------------------
# Draft engine lifecycle
# ---------------------------------------------------------------------------

@router.post("/start", summary="Initialize draft engine and state manager")
async def start_draft(req: StartDraftRequest):
    """
    Create DraftStateManager + LiveDraftEngine.
    Optionally connect the Playwright bridge if draft_room_url is provided.
    Registers the engine as an event callback on the bridge.
    """
    global _bridge, _engine, _state

    from backend.database import async_session
    from backend.engines.draft_state_manager import DraftStateManager
    from backend.engines.dependency_resolver import DependencyResolver
    from backend.engines.opponent_threat import OpponentThreatAnalyzer
    from backend.engines.live_draft import LiveDraftEngine

    if _engine is not None:
        return {"status": "already_started", "your_team_id": req.your_team_id}

    # Load user's league settings if league_id provided
    user_league = None
    if req.league_id:
        try:
            import uuid as _uuid
            from sqlalchemy import select
            from backend.models.user_league import UserLeague

            async with async_session() as session:
                result = await session.execute(
                    select(UserLeague).where(
                        UserLeague.id == _uuid.UUID(req.league_id)
                    )
                )
                user_league = result.scalar_one_or_none()
        except Exception as exc:
            logger.warning(
                "Could not load league %s for draft config: %s",
                req.league_id, exc,
            )

    config = DraftStateManager.config_from_user_league(user_league)
    _state = DraftStateManager(config, req.your_team_id)

    resolver = DependencyResolver()

    # Load historical manager tendencies from league auction data
    tendencies: dict = {}
    try:
        from backend.engines.league_auction import load_manager_tendencies
        async with async_session() as session:
            tendencies = await load_manager_tendencies(session)
        if tendencies:
            logger.info("Loaded tendencies for %d managers", len(tendencies))
    except Exception as exc:
        logger.warning("Could not load manager tendencies: %s", exc)

    threat_analyzer = OpponentThreatAnalyzer(tendencies=tendencies)

    _engine = LiveDraftEngine(
        state=_state,
        resolver=resolver,
        threat_analyzer=threat_analyzer,
        db_session_factory=async_session,
        ws_manager=ws_manager,
    )

    # Connect bridge if URL provided
    if req.draft_room_url:
        from backend.integrations.yahoo_playwright import YahooPlaywrightBridge

        _bridge = YahooPlaywrightBridge(ws_manager)
        _bridge.register_event_callback(_engine.handle_event)
        try:
            await _bridge.connect(req.draft_room_url)
        except Exception as exc:
            logger.error("Bridge connect failed during start: %s", exc)
            # Engine is still usable without bridge (manual frame injection)

    # If bridge already existed (from /draft/connect), register callback
    elif _bridge is not None:
        _bridge.register_event_callback(_engine.handle_event)

    logger.info("Draft engine started for team %s", req.your_team_id)
    return {"status": "started", "your_team_id": req.your_team_id}


@router.get("/state", summary="Current draft state snapshot")
async def get_draft_state():
    """Return budget, roster, and pick history."""
    _require_engine()
    return {
        "your_remaining_budget": _state.get_your_remaining_budget(),
        "spendable_on_next_player": _state.get_spendable_on_this_player(),
        "minimum_completion_budget": _state.get_minimum_completion_budget(),
        "roster_slots_remaining": _state.get_roster_slots_remaining(),
        "your_roster": [
            {
                "player_id": p.player_id,
                "player_name": p.player_name,
                "position": p.position,
                "price": p.price,
            }
            for p in _state.your_roster
        ],
        "total_picks": len(_state.picks),
        "positional_counts": _state.get_your_positional_counts(),
    }


@router.post("/frame", summary="Inject a frame into the engine")
async def inject_frame(req: FrameRequest):
    """
    Manually inject a draft event into the engine.
    Used for testing or when the bridge is not connected.
    """
    _require_engine()
    await _engine.handle_event(req.frame)
    return {"status": "processed", "event_type": req.frame.get("type")}


@router.get("/recommendation", summary="Last AI recommendation")
async def get_recommendation():
    """Return the most recent recommendation from the engine."""
    _require_engine()
    if _engine.last_recommendation is None:
        return {"status": "no_recommendation", "message": "No nomination processed yet"}
    return _engine.last_recommendation


@router.get("/opponents", summary="Opponent budget and threat data")
async def get_opponents():
    """Return opponent budgets, rosters, and combo alerts."""
    _require_engine()
    opponents = {}
    for team_id, roster in _state.opponent_rosters.items():
        budget = _state.opponent_budgets.get(team_id, 0)
        combos = _engine.threat_analyzer.get_active_combo_flags(roster)
        score = _engine.threat_analyzer.get_threat_score(roster, team_id=team_id)
        opponents[team_id] = {
            "budget": budget,
            "roster_count": len(roster),
            "threat_score": score,
            "combos": combos,
            "roster": [
                {
                    "player_name": p.player_name,
                    "position": p.position,
                    "price": p.price,
                }
                for p in roster
            ],
        }
    return {"opponents": opponents}


@router.post("/end", summary="Close draft session")
async def end_draft():
    """Tear down the engine and state. Does not disconnect the bridge."""
    global _engine, _state
    _engine = None
    _state = None
    logger.info("Draft session ended")
    return {"status": "ended"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_engine() -> None:
    if _engine is None or _state is None:
        raise HTTPException(
            status_code=409,
            detail="Draft engine not started — POST /draft/start first",
        )


def _require_bridge() -> None:
    if _bridge is None or not getattr(_bridge, "_connected", False):
        raise HTTPException(
            status_code=409,
            detail="Bridge not connected — POST /draft/connect first",
        )
