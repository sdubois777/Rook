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
