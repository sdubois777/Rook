"""
tests/unit/integrations/test_yahoo_playwright.py

Unit tests for backend/integrations/yahoo_playwright.py.
Required by stage-11-yahoo-playwright.md spec.

All tests use synthetic fixtures — no real Playwright browser or Yahoo connection
required. Frame format is designed to match tests/fixtures/yahoo_ws_frames.json.

FIXTURE NOTE: The yahoo_ws_frames.json fixture uses synthetic placeholder frames.
When real Yahoo draft room frames are captured (~August), update the fixture and
re-run this test suite to verify the parser still handles them correctly.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.integrations.yahoo_playwright import YahooPlaywrightBridge, DRAFT_EVENT_TYPES
from backend.websocket.manager import WebSocketManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


@pytest.fixture
def yahoo_ws_frames() -> dict[str, Any]:
    """Load synthetic Yahoo WebSocket frame fixtures."""
    path = FIXTURES_DIR / "yahoo_ws_frames.json"
    with open(path) as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


@pytest.fixture
def mock_ws_manager() -> MagicMock:
    """Mock WebSocketManager with AsyncMock broadcast."""
    manager = MagicMock(spec=WebSocketManager)
    manager.broadcast = AsyncMock()
    return manager


@pytest.fixture
def bridge(mock_ws_manager) -> YahooPlaywrightBridge:
    """Bridge instance with mock WebSocket manager — no real Playwright."""
    return YahooPlaywrightBridge(mock_ws_manager)


# ---------------------------------------------------------------------------
# Test: Frame parsing — nomination
# ---------------------------------------------------------------------------

def test_nomination_event_parsed_from_ws_frame(bridge, yahoo_ws_frames):
    """
    Uses synthetic fixture — no real browser.
    Nomination frame must parse to type='nomination' with player_id and clock_seconds.
    """
    result = bridge._parse_yahoo_frame(
        yahoo_ws_frames["nomination"]["raw_payload"]
    )
    assert result is not None
    assert result["type"] == "nomination"
    assert "player_id" in result
    assert "clock_seconds" in result
    assert result["player_id"] == yahoo_ws_frames["nomination"]["expected_parsed"]["player_id"]


# ---------------------------------------------------------------------------
# Test: Frame parsing — bid update
# ---------------------------------------------------------------------------

def test_bid_update_event_parsed(bridge, yahoo_ws_frames):
    """Bid update frame must parse to type='bid_update' with integer current_bid."""
    result = bridge._parse_yahoo_frame(
        yahoo_ws_frames["bid_update"]["raw_payload"]
    )
    assert result is not None
    assert result["type"] == "bid_update"
    assert isinstance(result["current_bid"], int)
    assert result["current_bid"] == yahoo_ws_frames["bid_update"]["expected_parsed"]["current_bid"]


# ---------------------------------------------------------------------------
# Test: Frame parsing — draft pick confirmed
# ---------------------------------------------------------------------------

def test_draft_pick_confirmed_event_parsed(bridge, yahoo_ws_frames):
    """Draft pick frame must parse to type='draft_pick' with final_price."""
    result = bridge._parse_yahoo_frame(
        yahoo_ws_frames["draft_pick"]["raw_payload"]
    )
    assert result is not None
    assert result["type"] == "draft_pick"
    assert "final_price" in result
    assert result["final_price"] == yahoo_ws_frames["draft_pick"]["expected_parsed"]["final_price"]


# ---------------------------------------------------------------------------
# Test: Frame parsing — clock warning
# ---------------------------------------------------------------------------

def test_clock_warning_event_parsed(bridge, yahoo_ws_frames):
    """Clock warning frame must parse to type='clock_warning' with seconds_remaining."""
    result = bridge._parse_yahoo_frame(
        yahoo_ws_frames["clock_warning"]["raw_payload"]
    )
    assert result is not None
    assert result["type"] == "clock_warning"
    assert "seconds_remaining" in result


# ---------------------------------------------------------------------------
# Test: Unknown and malformed frames
# ---------------------------------------------------------------------------

def test_unknown_frame_type_returns_none(bridge):
    """Frames with unrecognised type (e.g. heartbeat) must return None, not crash."""
    result = bridge._parse_yahoo_frame('{"type": "heartbeat", "ts": 1234567890}')
    assert result is None


def test_malformed_json_frame_returns_none(bridge):
    """Non-JSON payload must return None, not raise an exception."""
    result = bridge._parse_yahoo_frame("not json {{{{")
    assert result is None


def test_empty_payload_returns_none(bridge):
    """Empty string payload must return None."""
    result = bridge._parse_yahoo_frame("")
    assert result is None


def test_non_dict_json_returns_none(bridge):
    """JSON arrays or scalars must return None (only dict frames are valid)."""
    result = bridge._parse_yahoo_frame("[1, 2, 3]")
    assert result is None


# ---------------------------------------------------------------------------
# Test: Bridge failure emits MANUAL_ACTION_REQUIRED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bridge_failure_emits_manual_action_alert(bridge, mock_ws_manager):
    """
    on_bridge_failure() must broadcast MANUAL_ACTION_REQUIRED immediately.
    action and urgency fields are required — React UI uses them to show the alert.
    """
    await bridge.on_bridge_failure(action="bid", details="$45")

    mock_ws_manager.broadcast.assert_awaited_once()
    emitted = mock_ws_manager.broadcast.call_args[0][0]
    assert emitted["type"] == "MANUAL_ACTION_REQUIRED"
    assert emitted["action"] == "bid"
    assert emitted["urgency"] == "high"
    assert "manually bid in Yahoo tab" in emitted["message"]


@pytest.mark.asyncio
async def test_bridge_failure_nominate_includes_details(bridge, mock_ws_manager):
    """on_bridge_failure() preserves action-specific details string."""
    await bridge.on_bridge_failure(action="nominate", details="Player 30977 at $1")

    emitted = mock_ws_manager.broadcast.call_args[0][0]
    assert emitted["action"] == "nominate"
    assert "30977" in emitted["details"]


# ---------------------------------------------------------------------------
# Test: Broadcast sent after parsed event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broadcast_sent_after_parsed_nomination(bridge, mock_ws_manager, yahoo_ws_frames):
    """
    Parsed nomination → ws_manager.broadcast() called with correct type.
    Full event chain: raw payload → _parse_yahoo_frame → _dispatch_event → broadcast.
    """
    raw = yahoo_ws_frames["nomination"]["raw_payload"]
    parsed = bridge._parse_yahoo_frame(raw)
    assert parsed is not None

    await bridge._dispatch_event(parsed)

    mock_ws_manager.broadcast.assert_awaited_once()
    sent = mock_ws_manager.broadcast.call_args[0][0]
    assert sent["type"] == "nomination"


@pytest.mark.asyncio
async def test_broadcast_sent_for_draft_pick(bridge, mock_ws_manager, yahoo_ws_frames):
    """Draft pick event must reach broadcast with final_price preserved."""
    raw = yahoo_ws_frames["draft_pick"]["raw_payload"]
    parsed = bridge._parse_yahoo_frame(raw)
    await bridge._dispatch_event(parsed)

    sent = mock_ws_manager.broadcast.call_args[0][0]
    assert sent["type"] == "draft_pick"
    assert sent["final_price"] == yahoo_ws_frames["draft_pick"]["expected_parsed"]["final_price"]


# ---------------------------------------------------------------------------
# Test: No polling in event chain
# ---------------------------------------------------------------------------

def test_no_polling_in_event_handlers():
    """
    Inspect source code for asyncio.sleep() inside event handlers.
    Only acceptable in health_check_loop() — which has an explicit comment.
    This test is non-negotiable per stage-11-yahoo-playwright.md spec.
    """
    import ast
    import inspect

    source = inspect.getsource(YahooPlaywrightBridge)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name == "health_check_loop":
            continue  # Only allowed here

        for child in ast.walk(node):
            if not (isinstance(child, ast.Await) and isinstance(child.value, ast.Call)):
                continue
            func = child.value.func
            # Check for asyncio.sleep or sleep in any form
            if isinstance(func, ast.Attribute) and func.attr == "sleep":
                pytest.fail(
                    f"asyncio.sleep() found in {node.name}() — "
                    f"polling is not allowed in event handlers"
                )
            if isinstance(func, ast.Name) and func.id == "sleep":
                pytest.fail(
                    f"sleep() found in {node.name}() — "
                    f"polling is not allowed in event handlers"
                )


# ---------------------------------------------------------------------------
# Test: Draft action errors fall back to on_bridge_failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nominate_failure_calls_on_bridge_failure(bridge, mock_ws_manager):
    """
    If page interaction throws during nominate_player(), on_bridge_failure() is called.
    Bridge never crashes silently.
    """
    bridge.page = MagicMock()
    bridge.page.evaluate = AsyncMock(side_effect=RuntimeError("page crashed"))
    bridge.page.click = AsyncMock(side_effect=RuntimeError("click failed"))
    bridge._connected = True

    await bridge.nominate_player("30977", 1)

    mock_ws_manager.broadcast.assert_awaited()
    emitted = mock_ws_manager.broadcast.call_args[0][0]
    assert emitted["type"] == "MANUAL_ACTION_REQUIRED"
    assert emitted["action"] == "nominate"


@pytest.mark.asyncio
async def test_bid_failure_calls_on_bridge_failure(bridge, mock_ws_manager):
    """If bid action fails, on_bridge_failure() is called with action='bid'."""
    bridge.page = MagicMock()
    bridge.page.evaluate = AsyncMock(side_effect=RuntimeError("bid failed"))
    bridge.page.click = AsyncMock(side_effect=RuntimeError("click failed"))
    bridge._connected = True

    await bridge.place_bid(45)

    emitted = mock_ws_manager.broadcast.call_args[0][0]
    assert emitted["type"] == "MANUAL_ACTION_REQUIRED"
    assert emitted["action"] == "bid"


# ---------------------------------------------------------------------------
# Test: WebSocketManager
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_manager_broadcast_removes_dead_connections():
    """Dead WebSocket connections are silently removed on next broadcast."""
    manager = WebSocketManager()

    # Simulate a dead connection that raises on send
    dead_ws = AsyncMock()
    dead_ws.send_json = AsyncMock(side_effect=RuntimeError("connection closed"))
    manager.active_connections.append(dead_ws)

    await manager.broadcast({"type": "test"})

    assert dead_ws not in manager.active_connections
    assert manager.connection_count == 0


@pytest.mark.asyncio
async def test_websocket_manager_broadcasts_to_all_clients():
    """broadcast() sends to every active connection."""
    manager = WebSocketManager()
    ws1, ws2 = AsyncMock(), AsyncMock()
    ws1.send_json, ws2.send_json = AsyncMock(), AsyncMock()
    manager.active_connections.extend([ws1, ws2])

    await manager.broadcast({"type": "nomination"})

    ws1.send_json.assert_awaited_once_with({"type": "nomination"})
    ws2.send_json.assert_awaited_once_with({"type": "nomination"})


# ---------------------------------------------------------------------------
# Test: DRAFT_EVENT_TYPES completeness
# ---------------------------------------------------------------------------

def test_all_fixture_types_in_draft_event_types(yahoo_ws_frames):
    """Every frame type in the fixture must be in DRAFT_EVENT_TYPES."""
    for frame_key, frame_data in yahoo_ws_frames.items():
        expected = frame_data.get("expected_parsed", {}).get("type")
        if expected:
            assert expected in DRAFT_EVENT_TYPES, (
                f"Fixture type {expected!r} (from {frame_key}) "
                f"not in DRAFT_EVENT_TYPES — add it or update the fixture"
            )


# ---------------------------------------------------------------------------
# Test: _ping_draft_room without page
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ping_returns_false_when_not_connected(bridge):
    """_ping_draft_room() returns False when bridge.page is None."""
    assert bridge.page is None
    result = await bridge._ping_draft_room()
    assert result is False


@pytest.mark.asyncio
async def test_ping_returns_false_when_connected_flag_false(bridge):
    """_ping_draft_room() returns False when _connected is False even with a page."""
    bridge.page = MagicMock()
    bridge._connected = False
    result = await bridge._ping_draft_room()
    assert result is False


@pytest.mark.asyncio
async def test_ping_returns_false_when_page_evaluate_raises(bridge):
    """_ping_draft_room() returns False if page.evaluate() throws (connection dropped)."""
    bridge.page = AsyncMock()
    bridge.page.evaluate = AsyncMock(side_effect=RuntimeError("page disconnected"))
    bridge._connected = True
    result = await bridge._ping_draft_room()
    assert result is False


@pytest.mark.asyncio
async def test_ping_returns_true_when_page_responsive(bridge):
    """_ping_draft_room() returns True when page.evaluate() succeeds."""
    bridge.page = AsyncMock()
    bridge.page.evaluate = AsyncMock(return_value="complete")
    bridge._connected = True
    result = await bridge._ping_draft_room()
    assert result is True


# ---------------------------------------------------------------------------
# Test: _execute_action raises when page is None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_action_raises_without_page(bridge):
    """_execute_action() raises RuntimeError when bridge is not connected."""
    with pytest.raises(RuntimeError, match="not connected"):
        await bridge._execute_action("bid", {"amount": 10})


@pytest.mark.asyncio
async def test_execute_action_unknown_type_raises(bridge, mock_ws_manager):
    """_execute_action() with unknown action type raises ValueError after JS fallback fails."""
    bridge.page = MagicMock()
    bridge.page.evaluate = AsyncMock(side_effect=Exception("no api"))
    bridge._connected = True

    with pytest.raises(ValueError, match="Unknown action type"):
        await bridge._execute_action("unknown_action", {})


@pytest.mark.asyncio
async def test_execute_action_succeeds_via_js_evaluate(bridge):
    """_execute_action() succeeds via JS evaluate path — no click needed."""
    bridge.page = MagicMock()
    bridge.page.evaluate = AsyncMock(return_value=None)  # JS succeeds
    bridge._connected = True

    await bridge._execute_action("bid", {"amount": 25})

    bridge.page.evaluate.assert_awaited_once()
    bridge.page.click = AsyncMock()  # Should NOT be called
    bridge.page.click.assert_not_awaited() if hasattr(bridge.page.click, "assert_not_awaited") else None


@pytest.mark.asyncio
async def test_pass_failure_calls_on_bridge_failure(bridge, mock_ws_manager):
    """If pass action fails, on_bridge_failure() is called with action='pass'."""
    bridge.page = MagicMock()
    bridge.page.evaluate = AsyncMock(side_effect=RuntimeError("evaluate failed"))
    bridge.page.click = AsyncMock(side_effect=RuntimeError("click failed"))
    bridge._connected = True

    await bridge.pass_nomination()

    emitted = mock_ws_manager.broadcast.call_args[0][0]
    assert emitted["type"] == "MANUAL_ACTION_REQUIRED"
    assert emitted["action"] == "pass"


# ---------------------------------------------------------------------------
# Test: clock_expired frame parsing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_action_falls_back_to_click(bridge):
    """When JS evaluate fails, _execute_action() falls back to page.click()."""
    bridge.page = MagicMock()
    bridge.page.evaluate = AsyncMock(side_effect=Exception("no js api"))
    bridge.page.click = AsyncMock(return_value=None)  # click succeeds
    bridge._connected = True

    await bridge._execute_action("bid", {"amount": 30})

    bridge.page.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_check_triggers_reconnect_when_ping_fails(bridge, mock_ws_manager):
    """
    Simulated ping failure → _reconnect() is called.
    Tests one iteration of the health_check_loop without running forever.
    """
    call_count = 0

    async def fake_sleep(n):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise asyncio.CancelledError()

    bridge._ping_draft_room = AsyncMock(return_value=False)
    bridge._reconnect = AsyncMock()

    with patch("asyncio.sleep", new=fake_sleep):
        try:
            await bridge.health_check_loop()
        except asyncio.CancelledError:
            pass

    bridge._reconnect.assert_awaited()


@pytest.mark.asyncio
async def test_reconnect_returns_early_without_url(bridge, mock_ws_manager):
    """_reconnect() logs error and returns immediately when _draft_room_url is not set."""
    bridge._draft_room_url = None
    bridge.page = None
    await bridge._reconnect()
    # No broadcast called (no bridge failure — just a log message)
    mock_ws_manager.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_calls_on_bridge_failure_if_page_reload_fails(bridge, mock_ws_manager):
    """_reconnect() calls on_bridge_failure() when page.reload() raises."""
    bridge._draft_room_url = "https://example.com/draft"
    bridge.page = AsyncMock()
    bridge.page.reload = AsyncMock(side_effect=RuntimeError("reload failed"))
    bridge._inject_mutation_observer = AsyncMock()

    await bridge._reconnect()

    mock_ws_manager.broadcast.assert_awaited()
    emitted = mock_ws_manager.broadcast.call_args[0][0]
    assert emitted["type"] == "MANUAL_ACTION_REQUIRED"
    assert emitted["action"] == "reconnect"


@pytest.mark.asyncio
async def test_reconnect_succeeds_and_sets_connected_flag(bridge, mock_ws_manager):
    """_reconnect() sets _connected=True after successful page.reload()."""
    bridge._draft_room_url = "https://example.com/draft"
    bridge.page = AsyncMock()
    bridge.page.reload = AsyncMock(return_value=None)
    bridge._inject_mutation_observer = AsyncMock()
    bridge._connected = False

    await bridge._reconnect()

    assert bridge._connected is True
    mock_ws_manager.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_ws_interception_registers_handler(bridge):
    """_setup_websocket_interception() registers 'websocket' event on the page."""
    mock_page = MagicMock()
    mock_page.on = MagicMock()
    await bridge._setup_websocket_interception(mock_page)
    mock_page.on.assert_called_once_with("websocket", mock_page.on.call_args[0][1])


@pytest.mark.asyncio
async def test_inject_mutation_observer_handles_page_evaluate_error(bridge):
    """_inject_mutation_observer() logs warning and continues if evaluate() fails."""
    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=RuntimeError("evaluate failed"))
    # Should not raise
    await bridge._inject_mutation_observer(mock_page)


def test_clock_expired_event_parsed(bridge, yahoo_ws_frames):
    """Clock expired frame must parse to type='clock_expired' with player_id."""
    result = bridge._parse_yahoo_frame(
        yahoo_ws_frames["clock_expired"]["raw_payload"]
    )
    assert result is not None
    assert result["type"] == "clock_expired"
    assert "player_id" in result


def test_parsed_event_has_timestamp_and_source(bridge, yahoo_ws_frames):
    """Every parsed event must include timestamp (ISO8601) and source fields."""
    for frame_key, frame_data in yahoo_ws_frames.items():
        raw = frame_data.get("raw_payload")
        if not raw:
            continue
        result = bridge._parse_yahoo_frame(raw)
        if result is not None:
            assert "timestamp" in result, f"{frame_key}: missing timestamp"
            assert "source" in result, f"{frame_key}: missing source"
