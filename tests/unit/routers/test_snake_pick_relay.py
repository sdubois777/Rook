"""Snake pick relay: _record_snake_pick enriches the payload from the real
yahoo_player_id so the UI can match + remove the picked player (draftboard rows
have a UUID id + full name, but no yahoo_player_id; the DOM name is abbreviated).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import backend.routers.draft as draft


def _event(payload):
    return SimpleNamespace(type="snake_pick", platform="yahoo", payload=payload)


def test_record_snake_pick_enriches_payload(monkeypatch):
    player = SimpleNamespace(id="uuid-7", name="J.K. Dobbins", position="RB")
    monkeypatch.setattr(draft, "_resolve_player_by_yahoo_id", AsyncMock(return_value=player))
    engine = AsyncMock()
    monkeypatch.setattr(draft, "_engine", engine)

    payload = {
        "yahoo_player_id": "nfl.p.123",
        "player_name": "J. DOBBINS",  # abbreviated from the DOM
        "position": None,
        "picker": "You",
        "is_yours": True,
    }
    asyncio.run(draft._record_snake_pick(_event(payload)))

    # Payload is enriched in place with canonical id + full name + position.
    assert payload["id"] == "uuid-7"
    assert payload["player_name"] == "J.K. Dobbins"
    assert payload["position"] == "RB"
    # is_yours / picker are preserved for the UI's roster attribution.
    assert payload["is_yours"] is True

    engine.on_pick_confirmed.assert_awaited_once()
    sent = engine.on_pick_confirmed.await_args[0][0]
    assert sent["player_name"] == "J.K. Dobbins"
    assert sent["player_id"] == "nfl.p.123"


def test_record_snake_pick_unresolved_keeps_raw_payload(monkeypatch):
    # Unknown yahoo id (resolution returns None) — payload stays as-is, no crash.
    monkeypatch.setattr(draft, "_resolve_player_by_yahoo_id", AsyncMock(return_value=None))
    monkeypatch.setattr(draft, "_engine", AsyncMock())

    payload = {"yahoo_player_id": "nfl.p.999", "player_name": "X. UNKNOWN", "picker": "You"}
    asyncio.run(draft._record_snake_pick(_event(payload)))

    assert "id" not in payload
    assert payload["player_name"] == "X. UNKNOWN"


def test_record_snake_pick_no_engine_still_enriches(monkeypatch):
    # A redeploy may wipe the engine; enrichment must still run for the UI.
    player = SimpleNamespace(id="uuid-9", name="Bijan Robinson", position="RB")
    monkeypatch.setattr(draft, "_resolve_player_by_yahoo_id", AsyncMock(return_value=player))
    monkeypatch.setattr(draft, "_engine", None)

    payload = {"yahoo_player_id": "nfl.p.100", "player_name": "B. ROBINSON", "picker": "You"}
    asyncio.run(draft._record_snake_pick(_event(payload)))

    assert payload["id"] == "uuid-9"
    assert payload["player_name"] == "Bijan Robinson"
