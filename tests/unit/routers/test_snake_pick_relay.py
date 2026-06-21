"""Snake pick relay: _record_snake_pick enriches the payload by resolving the
abbreviated DOM name ("C. MCCAFFREY") to the canonical Player by NAME — our DB
yahoo_player_id is "nfl_<gsis>", a different id space from Yahoo's frame id, so
an id lookup can't work. find_by_name_fuzzy (via _resolve_player) handles the
abbreviation; the enriched full name + UUID id let the UI match + remove it.

CHANGED (session-isolation refactor): _record_snake_pick no longer reads module
globals _engine/_state — it takes explicit engine= and state= args (the per-user
session's engine/state). Behavior assertions are unchanged; only how the engine
and state are supplied changed.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import backend.routers.draft as draft


def _event(payload):
    return SimpleNamespace(type="snake_pick", platform="yahoo", payload=payload)


def test_record_snake_pick_enriches_from_abbreviated_name(monkeypatch):
    player = SimpleNamespace(id="uuid-7", name="Christian McCaffrey", position="RB")
    resolve = AsyncMock(return_value=player)
    monkeypatch.setattr(draft, "_resolve_player", resolve)
    engine = AsyncMock()
    state = MagicMock()

    payload = {
        "yahoo_player_id": "yahoo-internal-id",  # NOT our nfl_<gsis> id
        "player_name": "C. MCCAFFREY",           # abbreviated from the DOM
        "position": None,
        "picker": "You",
        "is_yours": True,
    }
    asyncio.run(draft._record_snake_pick(_event(payload), engine=engine, state=state))

    # Resolution is by NAME, not the frame's id.
    resolve.assert_awaited_once_with("C. MCCAFFREY")
    assert payload["id"] == "uuid-7"
    assert payload["player_name"] == "Christian McCaffrey"
    assert payload["position"] == "RB"
    assert payload["is_yours"] is True  # preserved for roster attribution

    engine.on_pick_confirmed.assert_awaited_once()
    sent = engine.on_pick_confirmed.await_args[0][0]
    assert sent["player_name"] == "Christian McCaffrey"


def test_record_snake_pick_unresolved_keeps_raw_payload(monkeypatch):
    # Name doesn't resolve — payload stays as-is, no crash, no enrichment.
    monkeypatch.setattr(draft, "_resolve_player", AsyncMock(return_value=None))

    payload = {"yahoo_player_id": "x", "player_name": "X. UNKNOWN", "picker": "You"}
    asyncio.run(
        draft._record_snake_pick(_event(payload), engine=AsyncMock(), state=MagicMock())
    )

    assert "id" not in payload
    assert payload["player_name"] == "X. UNKNOWN"


def test_record_snake_pick_records_name_for_exclusion(monkeypatch):
    # The enriched (canonical) name is recorded into state so the engine can
    # exclude it from your-turn recommendations.
    player = SimpleNamespace(id="uuid-7", name="Christian McCaffrey", position="RB")
    monkeypatch.setattr(draft, "_resolve_player", AsyncMock(return_value=player))
    state = MagicMock()

    payload = {
        "yahoo_player_id": "x", "player_name": "C. MCCAFFREY", "picker": "You",
        "is_yours": True, "pick_number": 12, "round": 1,
    }
    asyncio.run(draft._record_snake_pick(_event(payload), engine=AsyncMock(), state=state))

    # Recorded under the canonical name, with the is_yours flag carried through.
    state.record_snake_pick.assert_called_once()
    kw = state.record_snake_pick.call_args.kwargs
    assert kw["player_name"] == "Christian McCaffrey"
    assert kw["is_yours"] is True
    assert kw["pick_number"] == 12


def test_record_snake_pick_no_engine_still_enriches(monkeypatch):
    # A redeploy may leave no session; enrichment must still run for the UI.
    player = SimpleNamespace(id="uuid-9", name="Bijan Robinson", position="RB")
    monkeypatch.setattr(draft, "_resolve_player", AsyncMock(return_value=player))

    payload = {"yahoo_player_id": "x", "player_name": "B. ROBINSON", "picker": "You"}
    asyncio.run(draft._record_snake_pick(_event(payload), engine=None, state=None))

    assert payload["id"] == "uuid-9"
    assert payload["player_name"] == "Bijan Robinson"
