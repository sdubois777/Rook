"""Tests for the FantasyPros ADP fetch (JSON partner API).

The old DOM scrape (Playwright) was replaced by an anonymous JSON GET after FantasyPros'
2026 redesign bot-gated the ADP table. These tests mock the HTTP layer and verify the
parse/mapping + the same dict contract the consumers (sync_adp, format_market) rely on.
"""
from __future__ import annotations

import httpx
import pytest

from backend.integrations.fantasypros import _num, get_adp

# A minimal slice of the real payload shape (type=adp&position=ALL).
_PAYLOAD = {
    "players": [
        {"player_name": "Jahmyr Gibbs", "player_team_id": "DET", "player_position_id": "RB",
         "player_bye_week": "6", "rank_ecr": 1, "rank_ave": "1.0", "rank_min": "1", "rank_max": "1"},
        {"player_name": "Ja'Marr Chase", "player_team_id": "CIN", "player_position_id": "WR",
         "player_bye_week": "6", "rank_ecr": 3, "rank_ave": "3.0", "rank_min": "3", "rank_max": "3"},
        {"player_name": "Houston Texans", "player_team_id": "HOU", "player_position_id": "DST",
         "player_bye_week": "6", "rank_ecr": 180, "rank_ave": "182.5", "rank_min": "170", "rank_max": "195"},
        # rows that must be skipped: no rank, and no name
        {"player_name": "No Rank Guy", "player_position_id": "WR", "rank_ecr": None},
        {"player_name": "", "player_position_id": "RB", "rank_ecr": 50},
    ]
}


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _FakeClient:
    def __init__(self, payload, *, raise_on_get=False, **kw):
        self._p = payload
        self._raise = raise_on_get

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        if self._raise:
            raise httpx.ConnectError("boom")
        return _FakeResp(self._p)


def _patch_client(monkeypatch, payload=_PAYLOAD, raise_on_get=False):
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _FakeClient(payload, raise_on_get=raise_on_get, **kw),
    )


class TestNum:
    def test_coerces_int_str_none(self):
        assert _num(5) == 5.0
        assert _num("6") == 6.0
        assert _num("1.5") == 1.5
        assert _num(None) is None
        assert _num("") is None
        assert _num("n/a") is None


@pytest.mark.asyncio
async def test_get_adp_maps_fields_and_contract(monkeypatch):
    _patch_client(monkeypatch)
    rows = await get_adp("ppr")
    # 3 valid rows (the no-rank + no-name rows are skipped).
    assert len(rows) == 3
    gibbs = rows[0]
    assert set(gibbs) == {"rank", "name", "team", "position", "bye", "adp", "best", "worst", "scoring_format"}
    assert gibbs["rank"] == 1.0 and gibbs["name"] == "Jahmyr Gibbs"
    assert gibbs["team"] == "DET" and gibbs["position"] == "RB"
    assert gibbs["bye"] == 6.0 and gibbs["adp"] == 1.0
    assert gibbs["best"] == 1.0 and gibbs["worst"] == 1.0  # richer than the old scrape (was None)
    assert gibbs["scoring_format"] == "ppr"


@pytest.mark.asyncio
async def test_get_adp_maps_dst_to_def(monkeypatch):
    _patch_client(monkeypatch)
    rows = await get_adp("standard")
    dst = next(r for r in rows if r["name"] == "Houston Texans")
    assert dst["position"] == "DEF"   # our players table uses DEF, not DST


@pytest.mark.asyncio
async def test_get_adp_skips_rows_missing_rank_or_name(monkeypatch):
    _patch_client(monkeypatch)
    rows = await get_adp("ppr")
    names = {r["name"] for r in rows}
    assert "No Rank Guy" not in names   # null rank_ecr skipped
    assert "" not in names              # empty name skipped


@pytest.mark.asyncio
async def test_get_adp_non_fatal_on_http_failure(monkeypatch):
    """A bad pull returns [] (non-fatal), so a pipeline run keeps last-good values."""
    _patch_client(monkeypatch, raise_on_get=True)
    assert await get_adp("ppr") == []


@pytest.mark.asyncio
async def test_get_adp_rejects_unknown_format(monkeypatch):
    _patch_client(monkeypatch)
    with pytest.raises(ValueError):
        await get_adp("superflex")
