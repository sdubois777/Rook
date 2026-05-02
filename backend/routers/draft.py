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
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from backend.websocket.manager import ws_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/draft", tags=["draft"])

# Bridge instance — created on POST /draft/connect
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
# Internal helpers
# ---------------------------------------------------------------------------

def _require_bridge() -> None:
    if _bridge is None or not getattr(_bridge, "_connected", False):
        raise HTTPException(
            status_code=409,
            detail="Bridge not connected — POST /draft/connect first",
        )
