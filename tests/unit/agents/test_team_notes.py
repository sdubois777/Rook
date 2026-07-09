"""
Tests for system-notes regeneration (agents/team_notes.py, slice 3 Part 2) — the
prompt is fed ONLY real stored stats, and the note is written from the model's
response. Client is injected (no API call).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from backend.agents.team_notes import build_notes_input, regenerate_team_notes


class _TS:
    def __init__(self, team="DEN", season=2026):
        self.team_abbr = team
        self.season_year = season
        self.qb_name = "Bo Nix"
        self.qb_tier = "weak"
        self.rookie_qb_flag = False
        self.qb_cpoe = Decimal("-2.07")
        self.oc_scheme = "balanced"
        self.oc_run_pass_split_tendency = Decimal("0.593")
        self.sack_rate = Decimal("0.0346")
        self.run_block_stuff_rate = Decimal("0.082")
        self.pass_protection_grade = "A"
        self.run_blocking_grade = "C"
        self.system_grade = "C"
        self.personnel_tendency = "11"
        self.red_zone_philosophy = "spread"
        self.notes = "OLD HALLUCINATED NOTES"


def test_build_notes_input_contains_only_real_stored_stats():
    text = build_notes_input(_TS())
    # every real computed value the prose is allowed to cite appears in the input
    assert "3.5%" in text            # sack_rate 0.0346
    assert "8.2%" in text            # stuff_rate 0.082
    assert "59.3%" in text           # pass rate 0.593
    assert "-2.07" in text           # cpoe
    assert "grade A" in text and "grade C" in text
    assert "balanced" in text and "spread" in text and "Bo Nix" in text


class _Msg:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]


class _Msgs:
    def __init__(self, text):
        self._text = text
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        return _Msg(self._text)


class _Client:
    def __init__(self, text):
        self.messages = _Msgs(text)


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.commits = 0

    async def execute(self, *_a, **_k):
        return type("R", (), {"scalars": lambda _s: _Scalars(self._rows)})()

    async def commit(self):
        self.commits += 1


async def test_regenerate_writes_note_from_model_and_feeds_real_stats():
    ts = _TS()
    client = _Client("Denver's A-grade pass protection (3.5% sack rate) props up a weak Bo Nix.")
    db = _FakeDB([ts])
    res = await regenerate_team_notes(db, client=client)
    assert res["written"] == 1 and res["failed"] == 0
    assert ts.notes.startswith("Denver's A-grade")          # note replaced from the model
    # the model was fed the real-stats block (no fabrication possible)
    sent = client.messages.calls[0]["messages"][0]["content"]
    assert "3.5%" in sent and "-2.07" in sent
    assert "ONLY" in client.messages.calls[0]["system"]      # strict "use only provided" instruction


async def test_regenerate_loud_warns_on_failure(caplog):
    class _Boom:
        class messages:
            @staticmethod
            async def create(**kw):
                raise RuntimeError("api down")

    db = _FakeDB([_TS()])
    with caplog.at_level("WARNING"):
        res = await regenerate_team_notes(db, client=_Boom())
    assert res["failed"] == 1 and res["written"] == 0
    assert any("regeneration failed" in m for m in caplog.messages)
