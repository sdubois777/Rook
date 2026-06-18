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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from backend.core.dependencies import get_db
from backend.websocket.manager import ws_manager

# The Playwright bridge is an optional, legacy server-side control path. The
# browser extension now drives the draft room, so a missing Playwright install
# must not break this module — guard the import.
try:
    from backend.integrations.yahoo_playwright import YahooPlaywrightBridge
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

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
    draft_type: str | None = None  # overrides the league's draft_type if provided


class FrameRequest(BaseModel):
    frame: dict


class DraftEventPayload(BaseModel):
    # auction: nomination|bid_update|draft_pick|clock|my_bid|my_nomination
    # snake:   your_turn|your_turn_soon|snake_pick
    type: str
    platform: str    # yahoo|espn|sleeper
    payload: dict


# ---------------------------------------------------------------------------
# Extension relay — receives draft events via X-Draft-Token
# ---------------------------------------------------------------------------

@router.post("/event", summary="Relay draft event from browser extension")
async def relay_draft_event(
    event: DraftEventPayload,
    x_draft_token: str = Header(..., alias="X-Draft-Token"),
    db=Depends(get_db),
):
    """
    Receives draft events from the browser extension.
    Authenticates via X-Draft-Token header (not JWT).
    Broadcasts to React UI via WebSocket manager.
    """
    from backend.repositories.user_repo import UserRepository

    repo = UserRepository(db)
    user = await repo.get_by_draft_token(x_draft_token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid draft token")

    # Feed the live draft engine. A nomination needs a recommendation, so if the
    # engine singleton is missing (e.g. a Railway redeploy reset it mid-draft),
    # lazily build a default one rather than silently dropping the event. Picks
    # only update state, so they're recorded only when an engine already exists.
    if event.type == "nomination":
        if _engine is None:
            logger.warning(
                "Nomination received with no live draft engine — lazily "
                "initializing a default engine (a redeploy likely reset it). "
                "Call POST /draft/start for full budget/opponent fidelity."
            )
            await _build_engine()
        await _trigger_nomination(event)
    elif event.type == "your_turn":
        # Snake: user on the clock — recommend best available. Lazily build the
        # engine like a nomination so a redeploy mid-draft still yields a rec.
        if _engine is None:
            logger.warning(
                "your_turn received with no live draft engine — lazily "
                "initializing a default engine (a redeploy likely reset it)."
            )
            await _build_engine(draft_type="snake")
        await _trigger_your_turn(event)
    elif event.type == "snake_pick" and _engine is not None:
        await _record_snake_pick(event)
    elif event.type == "draft_pick" and _engine is not None:
        await _record_pick(event)
    elif event.type == "my_bid" and _state is not None:
        # Remember your own bid so a later sale whose winner the DOM poller
        # couldn't attribute ('unknown') can still be recovered as yours.
        _state.record_my_bid(
            event.payload.get("yahoo_player_id", ""),
            event.payload.get("amount"),
        )

    # Always relay the raw event so the UI updates (nomination card, bid,
    # clock, team budgets, pick log) regardless of engine state.
    await ws_manager.broadcast({
        "type": event.type,
        "payload": event.payload,
        "platform": event.platform,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return {"status": "relayed"}


async def _resolve_player(player_name: str):
    """Fuzzy-resolve a draft-room display name to a Player (or None)."""
    if not player_name:
        return None
    from backend.database import AsyncSessionLocal
    from backend.repositories.player_repo import PlayerRepository

    async with AsyncSessionLocal() as session:
        return await PlayerRepository(session).find_by_name_fuzzy(player_name)


async def _trigger_nomination(event: "DraftEventPayload") -> None:
    """Resolve the nominated player and run the engine's recommendation.

    The engine looks players up by yahoo_player_id and broadcasts the
    recommendation itself; an empty id falls back to its unknown-player pass.
    """
    player_name = event.payload.get("player_name", "")
    player = await _resolve_player(player_name)
    await _engine.on_nomination({
        "type": "nomination",
        "player_id": player.yahoo_player_id if player else "",
        "player_name": player_name,
    })


async def _record_pick(event: "DraftEventPayload") -> None:
    """Record a confirmed pick into engine state (updates opponent tracking).

    Recovers an unattributed win: when the DOM poller couldn't determine the
    winner ('unknown') but your last relayed bid matches this sale, attribute it
    to you — both in the engine (team_id -> your_team_id, so your budget/roster
    update) and in the payload (is_yours/winner), which is broadcast to the UI.
    """
    payload = event.payload
    player_name = payload.get("player_name", "")
    player = await _resolve_player(player_name)
    player_id = player.yahoo_player_id if player else ""
    winner = payload.get("winner", "")
    final_price = payload.get("final_price", 0) or 0

    if (
        winner == "unknown"
        and _state is not None
        and _state.is_my_winning_bid(player_id, final_price)
    ):
        winner = _state.your_team_id or winner
        payload["winner"] = winner
        payload["is_yours"] = True
        _state.last_my_bid = None  # consume so it can't attribute a second sale

    await _engine.on_pick_confirmed({
        "type": "draft_pick",
        "player_id": player_id,
        "team_id": winner,
        "final_price": final_price,
        "player_name": player_name,
        "position": player.position if player else "",
    })


async def _trigger_your_turn(event: "DraftEventPayload") -> None:
    """Snake: user is on the clock — run the best-available recommendation."""
    await _engine.on_your_turn({
        "type": "your_turn",
        "round": event.payload.get("round"),
        "pick": event.payload.get("pick"),
    })


async def _record_snake_pick(event: "DraftEventPayload") -> None:
    """Snake: record a confirmed pick into engine state.

    The Yahoo console.error frame carries the real yahoo_player_id and the DOM
    supplies the position, so no fuzzy name resolution is needed (snake-room
    names are abbreviated like "J. DOBBINS" and would resolve poorly).
    """
    payload = event.payload
    await _engine.on_pick_confirmed({
        "type": "draft_pick",
        "player_id": payload.get("yahoo_player_id", "") or "",
        "team_id": payload.get("picker", "") or "",
        "final_price": 0,
        "player_name": payload.get("player_name", "") or "",
        "position": payload.get("position", "") or "",
    })


async def _build_engine(
    your_team_id: str = "",
    league_id: str | None = None,
    draft_type: str | None = None,
) -> None:
    """Construct the DraftStateManager + LiveDraftEngine into module globals.

    Shared by POST /draft/start and the lazy-init path in POST /draft/event,
    so a recommendation can still be produced if the engine singleton was wiped
    by a container restart (Railway redeploy) mid-draft. league_id/your_team_id
    are optional: with neither, a default config + empty team id is used, which
    still yields valuation-driven recommendations (budget/opponent fidelity is
    degraded until POST /draft/start supplies the real identity).
    """
    global _engine, _state

    from backend.database import AsyncSessionLocal as async_session
    from backend.engines.draft_state_manager import DraftStateManager
    from backend.engines.dependency_resolver import DependencyResolver
    from backend.engines.opponent_threat import OpponentThreatAnalyzer
    from backend.engines.live_draft import LiveDraftEngine

    # Load user's league settings if a league_id was provided
    user_league = None
    if league_id:
        try:
            import uuid as _uuid
            from backend.repositories.league_repo import LeagueRepository

            async with async_session() as session:
                user_league = await LeagueRepository(session).get(
                    _uuid.UUID(league_id)
                )
        except Exception as exc:
            logger.warning(
                "Could not load league %s for draft config: %s", league_id, exc
            )

    config = DraftStateManager.config_from_user_league(user_league)
    # Explicit request override wins over the league's stored draft_type (e.g.
    # the frontend passing the selected league's type, or a manual snake start).
    if draft_type:
        config.draft_type = draft_type
    _state = DraftStateManager(config, your_team_id)

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

    if not PLAYWRIGHT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Playwright not available — use the DraftMind browser extension",
        )

    if _bridge and getattr(_bridge, "_connected", False):
        return {"status": "already_connected", "url": _bridge._draft_room_url}

    _bridge = YahooPlaywrightBridge(ws_manager)
    try:
        await _bridge.connect(req.draft_room_url)
        return {"status": "connected", "url": req.draft_room_url}
    except RuntimeError as exc:
        if "Chromium not installed" in str(exc):
            logger.error("Chromium not installed: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc))
        logger.error("Bridge connect failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Bridge connection failed: {exc}")
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
    Create DraftStateManager + LiveDraftEngine and mark the engine ready.

    No server-side browser is launched: the DraftMind browser extension drives
    the Yahoo draft room and relays events via POST /draft/event. The legacy
    Playwright bridge is intentionally not connected here (Yahoo's CSP blocks
    it and Chromium need not be installed).
    """
    if _engine is not None:
        return {
            "status": "ready",
            "mode": "extension",
            "team_name": req.your_team_id,
            "message": (
                "Draft engine already running. Make sure the DraftMind "
                "extension is active on the Yahoo draft page."
            ),
        }

    await _build_engine(req.your_team_id, req.league_id, req.draft_type)

    # No Playwright: the browser extension handles the draft room and relays
    # events via POST /draft/event. If a bridge was connected out-of-band via
    # POST /draft/connect, keep it wired so its frames still reach the engine.
    if _bridge is not None:
        _bridge.register_event_callback(_engine.handle_event)

    logger.info("Draft engine ready for team %s (extension mode)", req.your_team_id)
    return {
        "status": "ready",
        "mode": "extension",
        "team_name": req.your_team_id,
        "message": (
            "Draft engine ready. Make sure the DraftMind extension is "
            "active on the Yahoo draft page."
        ),
    }


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
