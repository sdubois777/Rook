"""
backend/routers/draft.py

Live draft endpoints — WebSocket push and HTTP action triggers.

Concurrency model (single-worker, in-memory, per-user):
  Each user's draft lives in its own session in `session_manager` (keyed by
  user_id), so concurrent drafts are fully isolated — no shared engine/state, no
  cross-broadcast. Each session is mirrored to the draft_sessions DB table on
  every event, so a redeploy/crash mid-draft rehydrates instead of losing it.

  - POST /draft/event   (extension)  X-Draft-Token -> user -> that user's session
  - WS   /ws/draft       (React)      ?token=<Clerk JWT> -> user -> that session
  - POST /draft/start|state|bid|nominate|pass|end|recommendation|opponents|frame
                         (React)      Clerk JWT (Depends(get_current_user))

NOTE: require_feature("live_draft") (the billing entitlement gate) will attach at
POST /draft/start and the WS connect — left as a marker; OUT OF SCOPE here.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from backend.core.dependencies import _verify_clerk_jwt, get_current_user, get_db
from backend.database import AsyncSessionLocal
from backend.models.user import User
from backend.services.draft_session import DbSessionStore, DraftSessionManager
from backend.websocket.manager import SessionScopedBroadcaster, ws_manager

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

# How recently a draft must have had a real EVENT (a pick/nomination → persist →
# draft_sessions.updated_at) for a page refresh to AUTO-RESUME it. Must comfortably
# span a FULL draft plus gaps: a 12-team snake/auction runs 2-3h, and a dropped
# extension connection (e.g. a Sleeper socket reconnect) can stall the event stream
# for a stretch with no persist — a 1h window made such a live draft fall out of
# "resumable" and a refresh 409'd to the empty board. 6h covers a real draft + its
# hiccups while still expiring a genuinely abandoned one; the explicit "End Draft"
# button is the immediate-forget signal. Env-overridable. Default 6h.
RESUME_WINDOW_SECONDS = int(os.environ.get("DRAFT_RESUME_WINDOW_SECONDS", 6 * 60 * 60))

# Legacy Playwright bridge — single optional server-side control path (not part
# of the per-user extension draft flow).
_bridge = None


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
# Engine / state construction (per session)
# ---------------------------------------------------------------------------


async def _build_state(
    your_team_id: str = "",
    league_id: str | None = None,
    draft_type: str | None = None,
):
    """Construct a DraftStateManager from a user's connected league (or defaults)."""
    from backend.engines.draft_state_manager import DraftStateManager

    user_league = None
    if league_id:
        try:
            from backend.repositories.league_repo import LeagueRepository

            async with AsyncSessionLocal() as session:
                user_league = await LeagueRepository(session).get(uuid.UUID(league_id))
        except Exception as exc:
            logger.warning("Could not load league %s for draft config: %s", league_id, exc)

    config = DraftStateManager.config_from_user_league(user_league)
    if draft_type:
        config.draft_type = draft_type
    return DraftStateManager(config, your_team_id)


async def _make_engine_for(state, session_key: str):
    """Engine factory passed to the session manager.

    Builds the resolver + threat analyzer + LiveDraftEngine, wired to a
    session-scoped broadcaster so the engine's broadcasts reach ONLY this user's
    WebSocket clients. The engine code is unchanged — it still calls
    `self.ws_manager.broadcast(...)`; that object is now session-scoped.
    """
    from backend.engines.dependency_resolver import DependencyResolver
    from backend.engines.live_draft import LiveDraftEngine
    from backend.engines.opponent_threat import OpponentThreatAnalyzer

    resolver = DependencyResolver()

    tendencies: dict = {}
    try:
        from backend.engines.league_auction import load_manager_tendencies
        async with AsyncSessionLocal() as session:
            tendencies = await load_manager_tendencies(session)
        if tendencies:
            logger.info("Loaded tendencies for %d managers", len(tendencies))
    except Exception as exc:
        logger.warning("Could not load manager tendencies: %s", exc)

    return LiveDraftEngine(
        state=state,
        resolver=resolver,
        threat_analyzer=OpponentThreatAnalyzer(tendencies=tendencies),
        db_session_factory=AsyncSessionLocal,
        ws_manager=SessionScopedBroadcaster(ws_manager, session_key),
    )


# The per-user session registry (warm engines) + durable DB mirror.
session_manager = DraftSessionManager(
    store=DbSessionStore(AsyncSessionLocal),
    engine_factory=_make_engine_for,
)


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
    Authenticates via X-Draft-Token header (not JWT) and routes to THAT user's
    isolated draft session. Broadcasts only to that user's WebSocket clients.
    """
    from backend.repositories.user_repo import UserRepository

    repo = UserRepository(db)
    user = await repo.get_by_draft_token(x_draft_token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid draft token")

    session_key = str(user.id)

    # Resolve this user's session. get_or_rehydrate restores a real mid-draft
    # state from the DB snapshot after a redeploy (replacing the old "build a
    # default engine" band-aid). nomination/your_turn lazily CREATE a default
    # session only when none exists at all.
    #
    # Engine processing (DB lookups + Sonnet recommendation + state persist) is
    # ISOLATED in this try: a failure here must NEVER suppress the raw-event
    # relay below. The room has to keep ticking — nominee on the block, timer,
    # bids, picks leaving the list, opponent budgets — even if the AI
    # recommendation or a state write throws; the rec simply arrives when it can.
    try:
        if event.type in ("nomination", "your_turn"):
            session = await session_manager.get_or_rehydrate(user.id)
            if session is None:
                logger.warning(
                    "%s with no session for user %s — creating a default session "
                    "(call POST /draft/start for full budget/opponent fidelity).",
                    event.type, session_key,
                )
                state = await _build_state(
                    draft_type="snake" if event.type == "your_turn" else None,
                )
                session = await session_manager.create(user.id, state)

            if event.type == "nomination":
                await _trigger_nomination(event, session.engine)
            else:
                await _trigger_your_turn(event, session.engine)
            await session_manager.persist(user.id)

        elif event.type == "snake_pick":
            # Enrich regardless of session so the UI can always match/remove the pick.
            session = await session_manager.get_or_rehydrate(user.id)
            await _record_snake_pick(
                event,
                engine=session.engine if session else None,
                state=session.state if session else None,
            )
            if session is not None:
                await session_manager.persist(user.id)

        elif event.type == "draft_pick":
            session = await session_manager.get_or_rehydrate(user.id)
            if session is not None:
                await _record_pick(event, session.engine, session.state)
                await session_manager.persist(user.id)

        elif event.type == "my_bid":
            session = await session_manager.get_or_rehydrate(user.id)
            if session is not None:
                session.state.record_my_bid(
                    event.payload.get("yahoo_player_id", ""),
                    event.payload.get("amount"),
                )
                await session_manager.persist(user.id)
    except Exception:
        logger.exception(
            "Engine processing failed for %s event (user %s) — relaying the raw "
            "event to the UI anyway",
            event.type, session_key,
        )

    # Always relay the raw event — but ONLY to this user's WebSocket clients.
    await ws_manager.broadcast_to_session(session_key, {
        "type": event.type,
        "payload": event.payload,
        "platform": event.platform,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return {"status": "relayed"}


async def _resolve_player(player_name: str, sleeper_id: str | None = None):
    """Resolve a draft event's player to a Player (or None).

    Sleeper sends a canonical `sleeper_id` (its pick/nomination frames are
    id-only), so try that exact, indexed match FIRST — `resolution_source=id_map`,
    the clean case. Yahoo/ESPN send no sleeper_id and fall straight through to the
    name-fuzzy path (`name_backstop`), so they're unaffected.
    """
    from backend.repositories.player_repo import PlayerRepository

    async with AsyncSessionLocal() as session:
        repo = PlayerRepository(session)
        if sleeper_id:
            by_id = await repo.find_by_sleeper_id(sleeper_id)
            if by_id is not None:
                return by_id
        if not player_name:
            return None
        return await repo.find_by_name_fuzzy(player_name)


async def _trigger_nomination(event: "DraftEventPayload", engine) -> None:
    """Resolve the nominated player and run the engine's recommendation.

    Sleeper nominations are id-only (`sleeper_player_id`, no name), so enrich the
    broadcast payload with the resolved name/id/position — otherwise the UI shows
    a blank nominee. Yahoo/ESPN already carry the name, so this is idempotent there.
    """
    payload = event.payload
    player_name = payload.get("player_name", "")
    player = await _resolve_player(player_name, payload.get("sleeper_player_id"))
    if player is not None:
        payload["player_name"] = player.name
        payload["player_id"] = str(player.id)
        if not payload.get("pos_team") and player.position:
            payload["pos_team"] = player.position
    await engine.on_nomination({
        "type": "nomination",
        "player_id": player.yahoo_player_id if player else "",
        "player_name": payload.get("player_name", "") or player_name,
    })


async def _record_pick(event: "DraftEventPayload", engine, state) -> None:
    """Record a confirmed pick into engine state (updates opponent tracking).

    Recovers an unattributed win: when the DOM poller couldn't determine the
    winner ('unknown') but your last relayed bid matches this sale, attribute it
    to you — in the engine (your budget/roster) and the broadcast payload.
    """
    payload = event.payload
    player_name = payload.get("player_name", "")
    player = await _resolve_player(player_name, payload.get("sleeper_player_id"))
    player_id = player.yahoo_player_id if player else ""
    winner = payload.get("winner", "")
    final_price = payload.get("final_price", 0) or 0

    # Enrich the broadcast payload so the UI can match + remove the player. The
    # React draft room shows an ABBREVIATED name ("T. McMillan") and Yahoo's own
    # id (a different id space than the board's canonical UUID), so the raw event
    # matches nothing. Resolve to the full name + canonical id (mirrors
    # _record_snake_pick).
    if player is not None:
        player_name = player.name  # full name for the engine's opponent roster too
        payload["player_name"] = player.name
        payload["player_id"] = str(player.id)
        payload["position"] = player.position or payload.get("position") or ""

    if (
        winner == "unknown"
        and state is not None
        and state.is_my_winning_bid(player_id, final_price)
    ):
        winner = state.your_team_id or winner
        payload["winner"] = winner
        payload["is_yours"] = True
        state.last_my_bid = None  # consume so it can't attribute a second sale

    await engine.on_pick_confirmed({
        "type": "draft_pick",
        "player_id": player_id,
        "team_id": winner,
        "final_price": final_price,
        "player_name": player_name,
        "position": player.position if player else "",
        "is_yours": bool(payload.get("is_yours")),
    })


async def _trigger_your_turn(event: "DraftEventPayload", engine) -> None:
    """Snake: user is on the clock — run the best-available recommendation."""
    await engine.on_your_turn({
        "type": "your_turn",
        "round": event.payload.get("round"),
        "pick": event.payload.get("pick"),
    })


async def _record_snake_pick(event: "DraftEventPayload", engine, state) -> None:
    """Snake: enrich the pick payload and record it into engine state.

    Resolve by NAME, not the console.error frame's id: our DB yahoo_player_id is
    "nfl_<gsis>", a different id space from Yahoo's frame id. The DOM 'Last:' name
    is abbreviated ("C. MCCAFFREY"); find_by_name_fuzzy handles that. The enriched
    full name + UUID id let the UI match + remove the picked player.
    """
    payload = event.payload
    abbreviated = payload.get("player_name", "") or ""

    player = await _resolve_player(abbreviated, payload.get("sleeper_player_id"))
    if player is not None:
        payload["id"] = str(player.id)
        payload["player_name"] = player.name
        payload["position"] = player.position or payload.get("position") or ""
    else:
        logger.warning("Snake pick: could not resolve player name %r", abbreviated)

    if state is not None:
        state.record_snake_pick(
            player_name=payload.get("player_name", "") or abbreviated,
            position=payload.get("position"),
            pick_number=payload.get("pick_number"),
            round_num=payload.get("round"),
            is_yours=bool(payload.get("is_yours", False)),
        )

    if engine is not None:
        await engine.on_pick_confirmed({
            "type": "draft_pick",
            "player_id": payload.get("yahoo_player_id", "") or "",
            "team_id": payload.get("picker", "") or "",
            "final_price": 0,
            "player_name": payload.get("player_name", "") or "",
            "position": payload.get("position", "") or "",
            "is_yours": bool(payload.get("is_yours")),
        })


# ---------------------------------------------------------------------------
# WebSocket endpoint — React clients connect here (per-session)
# ---------------------------------------------------------------------------

async def _resolve_ws_user_id(token: str | None) -> uuid.UUID | None:
    """Resolve a WS query token to a user.id (the session key).

    Production: verifies the Clerk JWT (short-lived; verified server-side, keeps
    the long-lived draft token off the wire). Dev (Clerk disabled): the token is
    treated as the external id. Returns None on any failure (connection rejected).
    """
    if not token:
        return None
    try:
        from backend.config import settings
        from backend.repositories.user_repo import UserRepository
        from backend.services.user_service import UserService

        if settings.clerk_enabled:
            payload = await _verify_clerk_jwt(token)
            external_id = payload["sub"]
            email = payload.get("email") or f"{external_id}@placeholder.local"
        else:
            external_id = token or "dev-user-001"
            email = f"{external_id}@dev.local"

        async with AsyncSessionLocal() as db:
            service = UserService(UserRepository(db))
            user, _ = await service.get_or_create(external_id=external_id, email=email)
            return user.id
    except Exception as exc:
        logger.warning("WS token resolution failed: %s", exc)
        return None


@router.websocket("/ws/draft")
async def draft_websocket(websocket: WebSocket, token: str | None = None):
    """
    Push-based WebSocket for React draft clients, scoped to the user's session.
    The client connects with ?token=<Clerk JWT>; the connection only receives
    that user's own draft events. No polling.
    """
    user_id = await _resolve_ws_user_id(token)
    if user_id is None:
        # 4401 = application-level "unauthorized" close code.
        await websocket.close(code=4401)
        logger.info("Draft WS rejected — missing/invalid token")
        return

    session_key = str(user_id)
    await ws_manager.connect(websocket, session_key=session_key)
    logger.info("Draft WebSocket client connected (session=%s)", session_key)
    try:
        while True:
            data = await websocket.receive_json()
            logger.debug("Client message: %s", data)
            # No client → server messages needed in current design
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        logger.info("Draft WebSocket client disconnected (session=%s)", session_key)
    except Exception as exc:
        logger.error("Draft WebSocket error: %s", exc)
        ws_manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# Bridge lifecycle (legacy Playwright — single optional path)
# ---------------------------------------------------------------------------

@router.post("/connect", summary="Connect Playwright bridge to Yahoo draft room")
async def connect_bridge(req: ConnectRequest):
    """Launch Playwright and connect to the Yahoo draft room (legacy/optional)."""
    global _bridge

    if not PLAYWRIGHT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Playwright not available — use the Rook browser extension",
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
# Draft action endpoints (legacy bridge — now authenticated)
# ---------------------------------------------------------------------------

@router.post("/bid", summary="Place a bid on the currently nominated player")
async def place_bid(req: BidRequest, user: User = Depends(get_current_user)):
    """Submit a bid. Bridge must be connected (POST /draft/connect first)."""
    _require_bridge()
    await _bridge.place_bid(req.amount)
    return {"status": "bid_placed", "amount": req.amount}


@router.post("/nominate", summary="Nominate a player for auction")
async def nominate_player(req: NominateRequest, user: User = Depends(get_current_user)):
    """Nominate a player. Bridge must be connected."""
    _require_bridge()
    await _bridge.nominate_player(req.yahoo_player_id, req.opening_bid)
    return {
        "status": "nominated",
        "yahoo_player_id": req.yahoo_player_id,
        "opening_bid": req.opening_bid,
    }


@router.post("/pass", summary="Pass on the current nomination")
async def pass_nomination(user: User = Depends(get_current_user)):
    """Pass on the current nomination. Bridge must be connected."""
    _require_bridge()
    await _bridge.pass_nomination()
    return {"status": "passed"}


# ---------------------------------------------------------------------------
# Draft engine lifecycle (per-user session)
# ---------------------------------------------------------------------------

@router.post("/start", summary="Initialize draft engine and state manager")
async def start_draft(req: StartDraftRequest, user: User = Depends(get_current_user)):
    """
    Create the current USER's draft session (DraftStateManager + LiveDraftEngine).

    Per-user: a second user calling /start while another drafts creates THEIR OWN
    session — never attaches to someone else's. No server-side browser is launched;
    the Rook extension drives the room and relays via POST /draft/event.

    (require_feature("live_draft") will gate this endpoint — out of scope here.)
    """
    # Short-circuit ONLY when there's a genuinely RESUMABLE draft (active + a
    # recent event) — the same gate /state uses, so the two never disagree. Keying
    # on warm-presence instead was a regression: a just-abandoned mock's warm
    # session lingers in memory (not reaped for hours), so /start returned "already
    # running" without refreshing the row, then /state's recency gate 409'd the
    # stale row ("engine not started"). is_resumable also covers a cold-but-recent
    # draft after a redeploy (warm gone) — so /start won't wipe a live draft either.
    # Not resumable (stale/abandoned, or none) → fall through and create fresh,
    # which overwrites the stale DB row + replaces any lingering warm session.
    if await session_manager.is_resumable(user.id, RESUME_WINDOW_SECONDS):
        return {
            "status": "ready",
            "mode": "extension",
            "team_name": req.your_team_id,
            "message": (
                "Draft engine already running. Make sure the Rook "
                "extension is active on the Yahoo draft page."
            ),
        }

    state = await _build_state(req.your_team_id, req.league_id, req.draft_type)
    await session_manager.create(user.id, state)

    if _bridge is not None:
        session = session_manager.get_warm(user.id)
        _bridge.register_event_callback(session.engine.handle_event)

    logger.info("Draft session ready for user %s team %s", user.id, req.your_team_id)
    return {
        "status": "ready",
        "mode": "extension",
        "team_name": req.your_team_id,
        "message": (
            "Draft engine ready. Make sure the Rook extension is "
            "active on the Yahoo draft page."
        ),
    }


@router.get("/state", summary="Current draft state snapshot")
async def get_draft_state(user: User = Depends(get_current_user)):
    """Return budget, roster, and pick history for the current user's draft."""
    session = await _require_session(user)
    state = session.state
    # Snake picks live in _my_picks (your_roster is the auction roster — empty on a
    # snake draft), so a refresh restored an empty roster + lost the drafted-filter
    # for your own picks. Source your roster from the right place per draft type.
    if state.draft_type == "snake":
        your_roster = [
            {
                "player_id": "",
                "player_name": p.get("player_name", ""),
                "position": p.get("position"),
                "price": 0,
            }
            for p in state.get_my_roster()
        ]
    else:
        your_roster = [
            {
                "player_id": p.player_id,
                "player_name": p.player_name,
                "position": p.position,
                "price": p.price,
            }
            for p in state.your_roster
        ]
    return {
        "your_remaining_budget": state.get_your_remaining_budget(),
        "spendable_on_next_player": state.get_spendable_on_this_player(),
        "minimum_completion_budget": state.get_minimum_completion_budget(),
        "roster_slots_remaining": max(
            0, state.league_config.total_roster_size - len(your_roster)
        ),
        "your_roster": your_roster,
        "total_picks": len(state.picks),
        "positional_counts": state.get_your_positional_counts(),
    }


@router.post("/frame", summary="Inject a frame into the engine")
async def inject_frame(req: FrameRequest, user: User = Depends(get_current_user)):
    """Manually inject a draft event into the current user's engine (testing)."""
    session = await _require_session(user)
    await session.engine.handle_event(req.frame)
    await session_manager.persist(user.id)
    return {"status": "processed", "event_type": req.frame.get("type")}


@router.get("/recommendation", summary="Last AI recommendation")
async def get_recommendation(user: User = Depends(get_current_user)):
    """Return the most recent recommendation from the current user's engine."""
    session = await _require_session(user)
    if session.engine.last_recommendation is None:
        return {"status": "no_recommendation", "message": "No nomination processed yet"}
    return session.engine.last_recommendation


@router.get("/opponents", summary="Opponent budget and threat data")
async def get_opponents(user: User = Depends(get_current_user)):
    """Return opponent budgets, rosters, and combo alerts for the user's draft."""
    session = await _require_session(user)
    state, engine = session.state, session.engine
    opponents = {}
    for team_id, roster in state.opponent_rosters.items():
        budget = state.opponent_budgets.get(team_id, 0)
        combos = engine.threat_analyzer.get_active_combo_flags(roster)
        score = engine.threat_analyzer.get_threat_score(roster, team_id=team_id)
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
async def end_draft(user: User = Depends(get_current_user)):
    """Tear down the current user's session. Does not disconnect the bridge."""
    await session_manager.end(user.id)
    logger.info("Draft session ended for user %s", user.id)
    return {"status": "ended"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _require_session(user: User):
    """Resolve the user's RESUMABLE session, or 409.

    Recency-gated: only auto-resume a draft that's plausibly still live (active +
    a real event within RESUME_WINDOW_SECONDS). A stale/abandoned or explicitly
    ended draft 409s, so the frontend shows the board instead of resurrecting a
    finished draft. Checked BEFORE get_or_rehydrate, so a stale read never warms
    (and keeps alive) a dead session — closing the reads-keep-it-warm leak.

    NOTE: this gates the READ endpoints only (/state, /recommendation, /opponents,
    /frame). POST /draft/event is intentionally NOT gated — an incoming event is
    itself fresh activity and re-freshens updated_at (keep-alive).
    """
    if not await session_manager.is_resumable(user.id, RESUME_WINDOW_SECONDS):
        raise HTTPException(
            status_code=409,
            detail="Draft engine not started — POST /draft/start first",
        )
    session = await session_manager.get_or_rehydrate(user.id)
    if session is None:
        raise HTTPException(
            status_code=409,
            detail="Draft engine not started — POST /draft/start first",
        )
    return session


def _require_bridge() -> None:
    if _bridge is None or not getattr(_bridge, "_connected", False):
        raise HTTPException(
            status_code=409,
            detail="Bridge not connected — POST /draft/connect first",
        )
