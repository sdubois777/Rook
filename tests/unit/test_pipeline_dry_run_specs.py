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
