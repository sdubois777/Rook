"""Identity backfill for league_auction_history rows written by the Yahoo league sync.

This repair is pointed at PRODUCTION by hand and had no tests. The failure it exists to
undo — a real auction sitting in the table that no consumer can read — is silent, and so
are all three ways the repair itself can go wrong: binding a price to the wrong player,
aborting the whole commit on a constraint, and reporting success for rows that stay
unusable because they carry no price.

`plan_backfill` is pure, so these are real assertions about the decision logic rather
than assertions about mocks.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "backfill_auction_identity.py"
_spec = importlib.util.spec_from_file_location("backfill_auction_identity_under_test", _SCRIPT)
bf = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bf
_spec.loader.exec_module(bf)


def row(key, season=2025, source="sync_461.l.1.t.3", price=10, name="", position=""):
    """An auction row as the Yahoo league sync wrote it: a yahoo_player_key and
    nothing else — no player_id, empty player_name, empty position."""
    return SimpleNamespace(
        yahoo_player_key=key, season_year=season, source=source,
        price=price, player_id=None, player_name=name, position=position,
    )


def player(pid, yahoo_id, name="Some Player", position="RB"):
    return SimpleNamespace(id=pid, yahoo_id=yahoo_id, name=name, position=position)


# ---------------------------------------------------------------------------
# The happy path — the 152-of-180 recovery this exists for
# ---------------------------------------------------------------------------

def test_resolves_a_yahoo_key_to_its_player():
    lookup, ambiguous = bf.build_lookup([player("p1", "33963", "Jahmyr Gibbs", "RB")])
    plan, stats, per_season = bf.plan_backfill(
        [row("461.p.33963", price=56)], lookup, ambiguous, set())

    assert stats["resolved"] == 1
    assert stats["resolved_priced"] == 1
    assert stats["recovered_spend"] == 56.0
    (_, pid, name, position) = plan[0]
    assert (pid, name, position) == ("p1", "Jahmyr Gibbs", "RB")
    assert per_season[2025] == {"resolved": 1, "priced": 1, "spend": 56.0}


def test_league_prefix_is_ignored_only_the_player_id_suffix_matters():
    """Keys carry the league id: 461.p.33963 in one season, 423.p.33963 in another.
    Both must resolve to the same player."""
    lookup, ambiguous = bf.build_lookup([player("p1", "33963")])
    plan, stats, _ = bf.plan_backfill(
        [row("461.p.33963", season=2025), row("423.p.33963", season=2024)],
        lookup, ambiguous, set())
    assert stats["resolved"] == 2
    assert {p[1] for p in plan} == {"p1"}


# ---------------------------------------------------------------------------
# Never guess — the ways a price can land on the wrong player
# ---------------------------------------------------------------------------

def test_ambiguous_yahoo_id_is_skipped_not_guessed():
    """~54 duplicate player clusters exist. Two rows sharing a yahoo_id must resolve to
    NEITHER — attributing a real auction price to the wrong player is worse than
    leaving it unresolved."""
    lookup, ambiguous = bf.build_lookup([
        player("p1", "33963", "Frank Gore Sr"),
        player("p2", "33963", "Frank Gore Jr"),
    ])
    assert ambiguous == {"33963"}
    assert "33963" not in lookup

    plan, stats, _ = bf.plan_backfill([row("461.p.33963")], lookup, ambiguous, set())
    assert plan == []
    assert stats["ambiguous"] == 1
    assert stats["resolved"] == 0


def test_unknown_yahoo_id_is_left_alone():
    lookup, ambiguous = bf.build_lookup([player("p1", "33963")])
    plan, stats, _ = bf.plan_backfill([row("461.p.99999")], lookup, ambiguous, set())
    assert plan == []
    assert stats["unmatched"] == 1


@pytest.mark.parametrize("key", ["", None, "461.33963", "garbage"])
def test_a_key_without_the_p_marker_is_not_parsed(key):
    """Never fall back to splitting on '.' — a malformed key must not yield a suffix
    that happens to match some player's yahoo_id."""
    lookup, ambiguous = bf.build_lookup([player("p1", "33963")])
    plan, stats, _ = bf.plan_backfill([row(key)], lookup, ambiguous, set())
    assert plan == []
    assert stats["no_key"] == 1


# ---------------------------------------------------------------------------
# uq_auction_player_season_source — one IntegrityError rolls back the whole repair
# ---------------------------------------------------------------------------

def test_collision_with_an_existing_row_is_skipped():
    """NULL player_id never conflicts in Postgres, so a duplicate only collides at the
    instant identity is written. Committing it raises and loses the ENTIRE backfill."""
    lookup, ambiguous = bf.build_lookup([player("p1", "33963")])
    taken = {("p1", 2025, "sync_461.l.1.t.3")}
    plan, stats, _ = bf.plan_backfill([row("461.p.33963")], lookup, ambiguous, taken)
    assert plan == []
    assert stats["collision"] == 1


def test_two_idless_rows_claiming_one_slot_only_one_wins():
    lookup, ambiguous = bf.build_lookup([player("p1", "33963")])
    plan, stats, _ = bf.plan_backfill(
        [row("461.p.33963"), row("461.p.33963")], lookup, ambiguous, set())
    assert len(plan) == 1
    assert stats["resolved"] == 1
    assert stats["collision"] == 1


def test_same_player_different_season_is_not_a_collision():
    lookup, ambiguous = bf.build_lookup([player("p1", "33963")])
    plan, _stats, _ = bf.plan_backfill(
        [row("461.p.33963", season=2024), row("461.p.33963", season=2025)],
        lookup, ambiguous, set())
    assert len(plan) == 2


def test_taken_is_not_mutated_by_planning():
    """The planner must be replayable — a caller that plans twice should get the same
    answer, not a second run in which everything collides with the first."""
    lookup, ambiguous = bf.build_lookup([player("p1", "33963")])
    taken = set()
    bf.plan_backfill([row("461.p.33963")], lookup, ambiguous, taken)
    assert taken == set()
    plan2, stats2, _ = bf.plan_backfill([row("461.p.33963")], lookup, ambiguous, taken)
    assert len(plan2) == 1 and stats2["collision"] == 0


# ---------------------------------------------------------------------------
# Identity is not enough — a resolved row with no price is still unusable
# ---------------------------------------------------------------------------

def test_price_zero_resolves_but_does_not_count_as_priceable():
    """Yahoo omits `cost` for snake drafts, so the sync stores price 0. The backtest
    filters on price > 0, so these rows stay unscoreable however well they resolve.
    Reporting them as 'resolved' without this distinction has misled a reader before."""
    lookup, ambiguous = bf.build_lookup([player("p1", "33963")])
    plan, stats, per_season = bf.plan_backfill(
        [row("461.p.33963", price=0)], lookup, ambiguous, set())

    assert len(plan) == 1                      # identity IS repaired
    assert stats["resolved"] == 1
    assert stats["resolved_priced"] == 0       # but it buys the backtest nothing
    assert stats["recovered_spend"] == 0.0
    assert per_season[2025]["priced"] == 0


def test_recovered_spend_counts_only_priced_rows():
    lookup, ambiguous = bf.build_lookup([
        player("p1", "1"), player("p2", "2"), player("p3", "3"),
    ])
    plan, stats, _ = bf.plan_backfill(
        [row("461.p.1", price=56), row("461.p.2", price=0), row("461.p.3", price=4)],
        lookup, ambiguous, set())
    assert len(plan) == 3
    assert stats["resolved"] == 3
    assert stats["resolved_priced"] == 2
    assert stats["recovered_spend"] == 60.0


# ---------------------------------------------------------------------------
# The report is the deliverable — it must answer "can the backtest read this season?"
# ---------------------------------------------------------------------------

def test_report_usable_names_the_threshold_verdict(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="backfill_auction_identity"):
        bf._report_usable({2025: 3}, {2025: 155}, 50)
    out = "\n".join(r.getMessage() for r in caplog.records)
    assert "3 -> 155" in out
    assert "USES LEAGUE PRICES" in out


def test_report_usable_says_so_when_still_short(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="backfill_auction_identity"):
        bf._report_usable({2023: 0}, {2023: 0}, 50)
    out = "\n".join(r.getMessage() for r in caplog.records)
    assert "falls through to ADP" in out


# ---------------------------------------------------------------------------
# The prod-invocation trap this change exists to close
# ---------------------------------------------------------------------------

def test_docstring_does_not_document_an_env_var_the_script_ignores():
    """The script connects via backend.database.AsyncSessionLocal (settings.database_url,
    i.e. DATABASE_URL / ROOK_ENV_FILE). It does NOT read PROD_DATABASE_URL, which sibling
    scripts do. Documenting that variable here sent the 'prod repair' to dev, where it
    found nothing to fix and printed a clean bill of health for a database it never
    opened."""
    src = _SCRIPT.read_text(encoding="utf-8")
    doc = src.split('"""')[1]
    assert "ROOK_ENV_FILE=.env.prod" in doc, "prod invocation must name the real switch"
    assert "PROD_DATABASE_URL" not in src.split("This docstring used to say")[0], (
        "PROD_DATABASE_URL is not read by this script; documenting it targets the "
        "wrong database silently"
    )
