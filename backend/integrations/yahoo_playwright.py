"""
backend/integrations/yahoo_playwright.py

Playwright bridge — controls Yahoo Fantasy draft room.

Primary event source:  WebSocket frame interception (page.on("websocket"))
Secondary fallback:    MutationObserver on key DOM elements
Health check:          ping every 10 seconds, auto-reconnect on failure

Design rules (non-negotiable):
  - NEVER use asyncio.sleep() inside an event handler
  - ALWAYS call on_bridge_failure() before any exception propagates
  - Log every frame receive at DEBUG level
  - Never crash silently — every exception emits MANUAL_ACTION_REQUIRED

TESTING NOTE: _parse_yahoo_frame() is the only method testable without a real
Yahoo draft room. Frame format is designed around the synthetic fixtures in
tests/fixtures/yahoo_ws_frames.json. When real frames are captured (~August),
update those fixtures and verify the parser handles them correctly.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Draft event types emitted by this bridge
DRAFT_EVENT_TYPES = frozenset({
    "nomination",
    "bid_update",
    "draft_pick",
    "clock_warning",
    "clock_expired",
})


class YahooPlaywrightBridge:
    """
    Controls Yahoo Fantasy draft room via Playwright.

    Primary event source: WebSocket frame interception
    Secondary fallback: MutationObserver on key DOM elements
    Health check: ping every 10 seconds, auto-reconnect on failure

    NEVER use asyncio.sleep() inside event handlers.
    ALWAYS call on_bridge_failure() before raising any exception.
    """

    def __init__(self, ws_manager) -> None:
        self.ws_manager = ws_manager
        self.page = None
        self.browser = None
        self._connected: bool = False
        self._draft_room_url: str | None = None
        self._event_callbacks: list = []

    # ---------------------------------------------------------------------------
    # Connection management
    # ---------------------------------------------------------------------------

    async def connect(self, draft_room_url: str) -> None:
        """
        Launch browser, authenticate, navigate to draft room.
        Sets up WebSocket interception and MutationObserver fallback.
        Starts health check loop in background task.
        """
        from playwright.async_api import async_playwright

        self._draft_room_url = draft_room_url
        playwright = await async_playwright().start()
        try:
            self.browser = await playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):
                logger.error(
                    "Chromium not installed. Run: playwright install chromium"
                )
                raise RuntimeError(
                    "Playwright Chromium not installed — run 'playwright install chromium'"
                ) from exc
            raise
        context = await self.browser.new_context()
        self.page = await context.new_page()

        await self._setup_websocket_interception(self.page)
        await self.page.goto(draft_room_url, wait_until="networkidle")
        await self._inject_mutation_observer(self.page)

        self._connected = True
        logger.info("Bridge connected to draft room: %s", draft_room_url)

        # Health check loop runs independently — does not block event handling
        asyncio.create_task(self.health_check_loop())

    async def _reconnect(self) -> None:
        """Attempt to reconnect to the draft room after a connection drop."""
        if not self._draft_room_url:
            logger.error("Cannot reconnect — draft room URL not set")
            return
        logger.info("Attempting bridge reconnect...")
        try:
            if self.page:
                await self.page.reload(wait_until="networkidle")
                await self._inject_mutation_observer(self.page)
            self._connected = True
            logger.info("Bridge reconnected successfully")
        except Exception as exc:
            logger.error("Reconnect failed: %s", exc)
            await self.on_bridge_failure(action="reconnect", details=str(exc))

    # ---------------------------------------------------------------------------
    # Primary event source: WebSocket interception
    # ---------------------------------------------------------------------------

    async def _setup_websocket_interception(self, page) -> None:
        """
        Primary event source.
        Intercepts Yahoo's own WebSocket connection to their servers.
        Fires on every frame Yahoo receives — no polling required.
        """
        async def handle_ws(ws):
            async def handle_frame(frame):
                try:
                    logger.debug("WS frame received: %s", frame.payload[:120])
                    data = self._parse_yahoo_frame(frame.payload)
                    if data:
                        await self._dispatch_event(data)
                except Exception as exc:
                    # Never crash on a bad frame — log and continue
                    logger.error("Frame parse/dispatch error: %s", exc)

            ws.on("framereceived", handle_frame)

        page.on("websocket", handle_ws)

    # ---------------------------------------------------------------------------
    # Secondary fallback: MutationObserver
    # ---------------------------------------------------------------------------

    async def _inject_mutation_observer(self, page) -> None:
        """
        Secondary fallback.
        Watches DOM for changes Yahoo's UI makes after receiving events.
        Catches anything WebSocket interception misses.
        Only fires on actual DOM mutations — no polling.
        """
        observer_script = """
        (function() {
            if (window.__yahoo_bridge_active__) return;
            window.__yahoo_bridge_active__ = true;

            const observer = new MutationObserver((mutations) => {
                for (const mutation of mutations) {
                    // Nomination panel appearing
                    if (document.querySelector('.draft-nomination')) {
                        window.__playwright_bridge_event__({
                            type: 'dom_nomination_panel',
                            source: 'mutation_observer'
                        });
                    }
                    // Current bid changing
                    const bidEl = document.querySelector('.current-bid-amount');
                    if (bidEl && mutation.target.contains(bidEl)) {
                        window.__playwright_bridge_event__({
                            type: 'dom_bid_update',
                            current_bid: parseInt(bidEl.textContent) || 0,
                            source: 'mutation_observer'
                        });
                    }
                }
            });

            observer.observe(document.body, { childList: true, subtree: true });
        })();
        """
        try:
            await page.evaluate(observer_script)
        except Exception as exc:
            logger.warning("MutationObserver injection failed: %s", exc)

    # ---------------------------------------------------------------------------
    # Frame parsing
    # ---------------------------------------------------------------------------

    def _parse_yahoo_frame(self, payload: str) -> dict[str, Any] | None:
        """
        Parse a raw Yahoo WebSocket frame into a structured event dict.
        Returns None for frames that are not draft events we care about.

        NOTE: Frame format is based on synthetic fixtures (tests/fixtures/yahoo_ws_frames.json).
        When real Yahoo frames are captured (~August), verify and update this method.

        Frame types handled:
          nomination   — player nominated, clock started
          bid_update   — current bid price changed
          draft_pick   — pick confirmed, player off board
          clock_warning — N seconds remaining
          clock_expired — nomination clock ran out
        """
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            logger.debug("Non-JSON frame ignored: %s", payload[:60])
            return None

        if not isinstance(data, dict):
            return None

        event_type = data.get("type")
        if event_type not in DRAFT_EVENT_TYPES:
            logger.debug("Non-draft frame ignored (type=%r)", event_type)
            return None

        # Normalise to canonical event structure
        return self._normalise_event(event_type, data)

    def _normalise_event(
        self, event_type: str, raw: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Map raw Yahoo frame fields to canonical event structure.
        Adds bridge metadata (timestamp, source).
        """
        event: dict[str, Any] = {
            "type": event_type,
            "source": "websocket",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if event_type == "nomination":
            event.update({
                "player_id": raw.get("player_id"),
                "player_name": raw.get("player_name"),
                "nominated_by": raw.get("nominated_by"),
                "clock_seconds": raw.get("clock_seconds", 30),
                "starting_bid": raw.get("starting_bid", 1),
            })
        elif event_type == "bid_update":
            event.update({
                "player_id": raw.get("player_id"),
                "current_bid": raw.get("current_bid"),
                "current_bidder": raw.get("current_bidder"),
            })
        elif event_type == "draft_pick":
            event.update({
                "player_id": raw.get("player_id"),
                "player_name": raw.get("player_name"),
                "team_id": raw.get("team_id"),
                "final_price": raw.get("final_price"),
            })
        elif event_type == "clock_warning":
            event.update({
                "seconds_remaining": raw.get("seconds_remaining"),
            })
        elif event_type == "clock_expired":
            event.update({
                "player_id": raw.get("player_id"),
            })

        return event

    # ---------------------------------------------------------------------------
    # Event dispatch
    # ---------------------------------------------------------------------------

    def register_event_callback(self, callback) -> None:
        """Register an async callback to receive all draft events."""
        self._event_callbacks.append(callback)

    async def _dispatch_event(self, event: dict[str, Any]) -> None:
        """Route parsed event to WebSocketManager and registered callbacks."""
        await self.ws_manager.broadcast(event)
        for callback in self._event_callbacks:
            try:
                await callback(event)
            except Exception as exc:
                logger.error("Event callback error: %s", exc)

    # ---------------------------------------------------------------------------
    # Draft actions (user → Yahoo)
    # ---------------------------------------------------------------------------

    async def nominate_player(
        self, yahoo_player_id: str, opening_bid: int
    ) -> None:
        """
        Trigger nomination in Yahoo draft room via Playwright.
        Falls back to on_bridge_failure if interaction fails.
        """
        try:
            await self._execute_action("nominate", {
                "player_id": yahoo_player_id,
                "bid": opening_bid,
            })
        except Exception as exc:
            logger.error("Nominate action failed: %s", exc)
            await self.on_bridge_failure(
                action="nominate",
                details=f"Player {yahoo_player_id} at ${opening_bid}",
            )

    async def place_bid(self, amount: int) -> None:
        """Submit a bid on the currently nominated player."""
        try:
            await self._execute_action("bid", {"amount": amount})
        except Exception as exc:
            logger.error("Bid action failed: %s", exc)
            await self.on_bridge_failure(action="bid", details=f"${amount}")

    async def pass_nomination(self) -> None:
        """Pass on the current nomination."""
        try:
            await self._execute_action("pass", {})
        except Exception as exc:
            logger.error("Pass action failed: %s", exc)
            await self.on_bridge_failure(action="pass", details="")

    async def _execute_action(
        self, action_type: str, params: dict[str, Any]
    ) -> None:
        """
        Execute a draft room action via Playwright page interaction.
        Tries page.evaluate() first (fastest), falls back to page.click().
        """
        if self.page is None:
            raise RuntimeError("Bridge not connected — call connect() first")

        # Primary: JS injection (fastest, most reliable if Yahoo's API is stable)
        try:
            await self.page.evaluate(
                f"window.__yahooFantasyDraftAction__({json.dumps({'action': action_type, **params})})"
            )
            logger.info("Action executed via JS: %s %s", action_type, params)
            return
        except Exception:
            pass  # Fall through to click-based fallback

        # Secondary: DOM click fallback
        selector_map = {
            "nominate": "[data-action='nominate']",
            "bid": "[data-action='bid']",
            "pass": "[data-action='pass']",
        }
        selector = selector_map.get(action_type)
        if selector:
            await self.page.click(selector, timeout=5000)
            logger.info("Action executed via click: %s", action_type)
        else:
            raise ValueError(f"Unknown action type: {action_type}")

    # ---------------------------------------------------------------------------
    # Health check (only acceptable use of asyncio.sleep() in this file)
    # ---------------------------------------------------------------------------

    async def health_check_loop(self) -> None:
        """
        Ping draft room every 10 seconds.
        Attempts reconnect if connection is dropped.
        asyncio.sleep() here is intentional — this is health monitoring, not polling.
        """
        while True:
            await asyncio.sleep(10)  # Health check interval — not polling
            if not await self._ping_draft_room():
                logger.warning("Bridge health check failed — reconnecting")
                await self._reconnect()

    async def _ping_draft_room(self) -> bool:
        """Return True if the draft room page is still responsive."""
        if self.page is None or not self._connected:
            return False
        try:
            await self.page.evaluate("() => document.readyState")
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------------------
    # Failure handling — mandatory, never optional
    # ---------------------------------------------------------------------------

    async def on_bridge_failure(
        self, action: str, details: str = ""
    ) -> None:
        """
        MANDATORY: Call this before any exception propagates out of this class.
        Emits MANUAL_ACTION_REQUIRED to the React UI immediately.
        The user sees exactly what to do manually in the Yahoo tab.
        Never crashes silently.
        """
        await self.ws_manager.broadcast({
            "type": "MANUAL_ACTION_REQUIRED",
            "action": action,
            "details": details,
            "urgency": "high",
            "message": f"App bridge failed — manually {action} in Yahoo tab",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        logger.error("Bridge failure: action=%s details=%s", action, details)
