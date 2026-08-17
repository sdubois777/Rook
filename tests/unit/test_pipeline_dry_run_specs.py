"""Guard: every pipeline agent has a dry-run cost spec.

`run_predraft_pipeline.py --dry-run` looks up each PIPELINE_ORDER agent in
AGENT_SPECS. When an agent is added to the run path but not the spec table, the
dry-run KeyErrors (regression this test exists to prevent).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_predraft_pipeline.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_predraft_pipeline", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # module-level only; main() is __main__-guarded
    return module


def test_every_pipeline_agent_has_a_dry_run_spec():
    m = _load()
    missing = [a for a in m.PIPELINE_ORDER if a not in m.AGENT_SPECS]
    assert not missing, f"agents in PIPELINE_ORDER with no AGENT_SPECS entry: {missing}"


def test_dry_run_prints_without_error(capsys):
    """print_dry_run over the full pipeline must not raise (all specs resolvable)."""
    m = _load()
    m.print_dry_run(m.PIPELINE_ORDER, single_team=False)
    out = capsys.readouterr().out
    assert "Dry-Run Cost Estimate" in out
    assert "kicker_baseline" in out  # the agent whose missing spec first broke it


def test_agent_specs_have_required_fields():
    m = _load()
    required = {"model", "max_tokens", "est_input_tokens", "api_calls", "status", "description"}
    for name, spec in m.AGENT_SPECS.items():
        assert required <= spec.keys(), f"{name} missing fields: {required - spec.keys()}"


@pytest.mark.asyncio
async def test_full_sweep_force_is_threaded_to_roster_changes():
    """--full-sweep must reach roster_changes, or a deliberate regen is a silent no-op.

    run_all_teams passes skip_if_fresh=not force (roster_changes.py:1617), so a call
    that omits force skips every team analyzed inside ROSTER_CHANGES_STALENESS_DAYS (7)
    even under --full-sweep. Because replace_team() wipes a team's rows before writing,
    a regen against a cleared player_dependencies table would then repopulate NOTHING —
    no error, no flags, and a board that still looks plausible.

    This regression existed because player_profiles threaded force and roster_changes,
    two lines above it, did not.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    m = _load()
    agent = MagicMock()
    agent.run_all_teams = AsyncMock(return_value={})

    with patch("backend.agents.roster_changes.RosterChangesAgent", return_value=agent):
        await m.run_agent("roster_changes", None, force=True, warehouse=MagicMock())

    agent.run_all_teams.assert_awaited_once()
    assert agent.run_all_teams.await_args.kwargs.get("force") is True, (
        "run_agent must pass force through to RosterChangesAgent.run_all_teams; "
        "without it --full-sweep silently skips every fresh team"
    )


@pytest.mark.asyncio
async def test_force_defaults_false_leaves_incremental_skip_intact():
    """The incremental path is the cost model — force must not be forced on."""
    from unittest.mock import AsyncMock, MagicMock, patch

    m = _load()
    agent = MagicMock()
    agent.run_all_teams = AsyncMock(return_value={})

    with patch("backend.agents.roster_changes.RosterChangesAgent", return_value=agent):
        await m.run_agent("roster_changes", None, warehouse=MagicMock())

    assert agent.run_all_teams.await_args.kwargs.get("force") is False


def test_pipeline_order_is_derived_from_phases():
    """PIPELINE_ORDER must be a pure flatten of PHASES, never hand-maintained.

    The phase loop filters each phase by `a in agents`, where agents is PIPELINE_ORDER
    under --agent all. So a stage present in PHASES but missing from PIPELINE_ORDER is
    SILENTLY SKIPPED with no error. The two lists were previously maintained separately
    and had already drifted — PIPELINE_ORDER listed team_metrics 12th and team_notes
    13th while PHASES runs them at 1b and 6c, so the dry-run advertised an execution
    order the pipeline does not follow.
    """
    m = _load()
    assert m.PIPELINE_ORDER == [a for phase in m.PHASES for a in phase]


def test_pipeline_order_has_no_duplicates():
    """A stage listed twice would run twice under --agent all."""
    m = _load()
    assert len(m.PIPELINE_ORDER) == len(set(m.PIPELINE_ORDER))


def test_market_values_runs_before_everything_that_reads_a_price():
    """The board's PPR auction price must be refreshed before anything consumes it.

    players.market_value_fantasypros is what a PPR league sees in the market column
    (backend/routers/draftboard.py:417 -> backend/services/format_display.py:180), and
    its only writer is sync_market_values. That writer was in NO pipeline phase, so a
    full run never refreshed the displayed price — the reported "market prices are out
    of date". The 6b format_market stage scrapes a fresh PPR price too, but writes it
    to player_format_values, which load_format_rows deliberately skips for PPR.
    """
    m = _load()
    order = m.PIPELINE_ORDER
    assert "market_values" in order, (
        "market_values must be a pipeline phase, or the displayed PPR market price "
        "only ever changes when someone runs scripts/refresh_market_values.py by hand"
    )
    # player_profiles routes players to Sonnet on market value; valuation_agent's
    # value_gap is the bid ceiling minus this price.
    assert order.index("market_values") < order.index("player_profiles")
    assert order.index("market_values") < order.index("valuation")
    assert order.index("market_values") < order.index("valuation_agent")


@pytest.mark.asyncio
async def test_market_values_is_skipped_under_an_asof_clock():
    """A live scrape on a past-dated board would overwrite that season's real prices.

    Same reason sync_adp and format_market skip: the FantasyPros scrape is
    current-season only. The as-of market comes from market_value_historic via
    _seed_asof_market, and this stage would undo it.
    """
    from unittest.mock import AsyncMock, patch

    m = _load()
    sync = AsyncMock()

    with (
        patch("backend.utils.seasons.asof_active", return_value=True),
        patch("backend.engines.market_values.sync_market_values", sync),
    ):
        await m.run_agent("market_values", None, warehouse=None)

    sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_market_scrape_is_recorded_not_swallowed():
    """A scrape failure leaves the OLD prices, which look identical to fresh ones.

    The run must therefore say so at the end rather than only warn mid-log.
    """
    from unittest.mock import AsyncMock, patch

    m = _load()
    m._STAGE_FAILURES.clear()

    with (
        patch("backend.utils.seasons.asof_active", return_value=False),
        patch(
            "backend.engines.market_values.sync_market_values",
            AsyncMock(return_value={"error": "FantasyPros timed out"}),
        ),
    ):
        await m.run_agent("market_values", None, warehouse=None)

    assert any("market_values" in f for f in m._STAGE_FAILURES), (
        "a failed market scrape must be recorded so the end-of-run summary reports it"
    )
    m._STAGE_FAILURES.clear()


def test_grade_owner_runs_before_its_consumers():
    """team_metrics is the SOLE owner of the deterministic grades and must precede
    roster_changes and player_profiles, which read them."""
    m = _load()
    order = m.PIPELINE_ORDER
    assert order.index("team_metrics") < order.index("roster_changes")
    assert order.index("team_metrics") < order.index("player_profiles")
    assert order.index("team_systems") < order.index("team_metrics")
    # valuation chain
    assert order.index("player_profiles") < order.index("valuation")
    assert order.index("valuation") < order.index("valuation_agent")
    assert order[-1] == "availability", "availability must run last"
