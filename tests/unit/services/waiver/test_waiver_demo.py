"""Waiver demo seam — the env gate + deterministic faked FAAB (pure)."""
from __future__ import annotations

from backend.services.waiver.waiver_demo_source import (
    _demo_faab_remaining,
    waiver_demo_enabled,
    waiver_demo_enforce_gates,
)


def test_demo_gate_off_by_default(monkeypatch):
    monkeypatch.delenv("WAIVER_DEMO_MODE", raising=False)
    assert waiver_demo_enabled() is False


def test_demo_gate_truthy_values(monkeypatch):
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("WAIVER_DEMO_MODE", v)
        assert waiver_demo_enabled() is True
    monkeypatch.setenv("WAIVER_DEMO_MODE", "off")
    assert waiver_demo_enabled() is False


def test_enforce_gates_independent_flag(monkeypatch):
    monkeypatch.delenv("WAIVER_DEMO_ENFORCE_GATES", raising=False)
    assert waiver_demo_enforce_gates() is False
    monkeypatch.setenv("WAIVER_DEMO_ENFORCE_GATES", "true")
    assert waiver_demo_enforce_gates() is True


def test_faab_remaining_deterministic_and_bounded():
    a = _demo_faab_remaining("demo-team-0", 0)
    b = _demo_faab_remaining("demo-team-0", 0)
    assert a == b                      # deterministic (no randomness)
    assert all(_demo_faab_remaining(f"t{i}", i) >= 20 for i in range(12))  # floored
