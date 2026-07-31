"""The pipeline must refresh platform ids, and must do it safely.

`players.espn_id` and `players.yahoo_id` are how ESPN and Sleeper league data is
matched to a player. Before this stage existed, `scripts/backfill_platform_ids.py`
had no caller anywhere in the repository, so those columns were only ever
populated by someone running the script by hand. Every rookie and mid-season
signing inserted by the seed step arrived with both columns empty and stayed
unresolvable until the next manual run.

These tests read the pipeline source directly rather than executing it, because
running the real pipeline needs a database, the network and paid model calls.
"""
from __future__ import annotations

from pathlib import Path

import pytest

PIPELINE = (
    Path(__file__).resolve().parents[3] / "scripts" / "run_predraft_pipeline.py"
)
SRC = PIPELINE.read_text(encoding="utf-8")


def test_the_pipeline_runs_the_platform_id_refresh():
    assert "backfill_platform_ids.py" in SRC, (
        "the pipeline no longer refreshes players.espn_id / players.yahoo_id, so "
        "newly seeded players will not resolve for ESPN or Sleeper league sync"
    )


def test_the_refresh_runs_after_the_roster_sync():
    """Order matters: seed and sync_rosters insert this season's new players, and
    the refresh has to see those rows to fill their ids."""
    assert SRC.index("scripts/sync_rosters.py") < SRC.index(
        "scripts/backfill_platform_ids.py"
    ), "the platform id refresh must run AFTER sync_rosters, not before"


def test_the_automatic_run_does_not_pass_the_repair_flag():
    """`--repair` overwrites an existing id with a different value.

    Filling an empty column is safe to do unattended. Rewriting an identity that
    is already set is not, so correcting values stays a deliberate manual act.
    """
    import re

    # Inspect the actual argument list handed to subprocess.run, not a text
    # window — a nearby comment explaining why the flag is omitted would
    # otherwise trip a substring check.
    call = re.search(
        r"subprocess\.run\(\s*\[([^\]]*backfill_platform_ids\.py[^\]]*)\]",
        SRC, re.S,
    )
    assert call, "could not find the subprocess.run call for the id refresh"
    args = call.group(1)
    assert "--repair" not in args, (
        f"the pipeline passes --repair, which lets an unattended run overwrite "
        f"player identity. Arguments: {args.strip()}"
    )


def test_a_refresh_failure_does_not_stop_the_pipeline():
    """Stale platform ids degrade league matching. They do not invalidate the
    board, so a failure here must not abort a run that costs real model spend."""
    start = SRC.index("scripts/backfill_platform_ids.py")
    window = SRC[start:start + 600]
    assert "returncode != 0" in window, "the refresh result is not checked at all"
    assert "WARNING" in window, (
        "a refresh failure should warn and continue, not pass silently"
    )
    assert "sys.exit" not in window and "raise" not in window, (
        "a platform id refresh failure must not abort the pipeline"
    )


def test_the_script_does_not_write_any_valuation_column():
    """The hard constraint. This stage runs inside the pipeline, so if it could
    write a valuation column it could move every bid ceiling on the board."""
    script = (
        Path(__file__).resolve().parents[3]
        / "scripts" / "backfill_platform_ids.py"
    ).read_text(encoding="utf-8")
    body = script.split('"""', 2)[-1]  # skip the module docstring
    for column in (
        "market_value", "ai_bid_ceiling", "recommended_bid_ceiling",
        "baseline_value", "adjusted_points",
    ):
        assert column not in body, (
            f"scripts/backfill_platform_ids.py references {column}; a pipeline "
            "stage that touches a valuation column can rescale the whole board"
        )
