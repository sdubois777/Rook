"""Tests for backend/repositories/player_repo.py — the draftable display filter."""
from __future__ import annotations

from sqlalchemy.dialects import postgresql

from backend.repositories.player_repo import draftable_filter


def _sql(expr) -> str:
    """Compile a SQLAlchemy expression to a literal-bound Postgres SQL string."""
    return str(expr.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))


def test_draftable_filter_is_adp_or_ceiling_above_floor():
    """A player is draftable if they have a FantasyPros ADP OR a ceiling > 1.

    This hides $1-floor players with no ADP (Roethlisberger) while keeping
    legit cheap players (a $3 sleeper) and all higher-valued players.
    """
    sql = _sql(draftable_filter()).lower()
    assert "market_value_fantasypros is not null" in sql
    assert "ai_bid_ceiling > 1" in sql
    assert " or " in sql


def test_draftable_filter_hides_dollar_floor_no_adp():
    """The >1 (not >=1) threshold is what excludes a $1-floor, no-ADP player."""
    sql = _sql(draftable_filter())
    # The ceiling condition must be strictly greater than 1 — a $1 ceiling
    # fails it, so a no-ADP $1 player is excluded.
    assert "ai_bid_ceiling > 1" in sql
    assert "ai_bid_ceiling >= 1" not in sql


def test_draftable_filter_keeps_valued_kdef():
    """K/DEF are $1 streamers (ai_bid_ceiling == 1, no FP ADP), so the generic
    gate would hide them — a dedicated clause keeps any VALUED (tier-set) K/DEF
    draftable so they appear on the board."""
    sql = _sql(draftable_filter()).lower()
    assert "position in ('k', 'def')" in sql
    assert "tier is not null" in sql
