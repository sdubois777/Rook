"""
scripts/run_predraft_pipeline.py

Runs the pre-draft AI agent pipeline in order.

Usage:
    uv run python scripts/run_predraft_pipeline.py --dry-run
    uv run python scripts/run_predraft_pipeline.py --agent all
    uv run python scripts/run_predraft_pipeline.py --agent team_systems
    uv run python scripts/run_predraft_pipeline.py --agent roster_changes --team LAC
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from pathlib import Path

# Ensure project root is on path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Agent specs — used for dry-run estimates and dispatch
# ---------------------------------------------------------------------------

AGENT_SPECS: dict[str, dict] = {
    "team_systems": {
        "model": "haiku",
        "model_id": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "est_input_tokens": 300,
        "api_calls": 32,
        "status": "built",
        "description": "NFL team systems (OC scheme, QB tier, O-line grades)",
    },
    "roster_changes": {
        "model": "sonnet",
        "model_id": "claude-sonnet-4-6",
        "max_tokens": 2000,
        "est_input_tokens": 800,
        "api_calls": 32,
        "status": "built",
        "description": "Player dependency flags (DISPLACED, CONTINGENT, etc.)",
    },
    "player_profiles": {
        "model": "haiku",
        "model_id": "claude-haiku-4-5-20251001",
        "max_tokens": 1000,
        "est_input_tokens": 500,
        "api_calls": 32,
        "status": "built",
        "description": "Player role classification and efficiency metrics",
    },
    "injury_risk": {
        "model": "haiku",
        "model_id": "claude-haiku-4-5-20251001",
        "max_tokens": 1000,
        "est_input_tokens": 400,
        "api_calls": 32,
        "status": "built",
        "description": "Injury risk profiles and risk-adjusted value modifiers",
    },
    "schedule": {
        "model": "haiku",
        "model_id": "claude-haiku-4-5-20251001",
        "max_tokens": 1500,
        "est_input_tokens": 400,
        "api_calls": 32,
        "status": "built",
        "description": "Schedule grades (early/full/playoff windows)",
    },
    "beat_reporter": {
        "model": "haiku",
        "model_id": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "est_input_tokens": 200,
        "api_calls": None,  # variable — RSS feed-driven
        "status": "built",
        "description": "Beat reporter signals (daily RSS ingestion)",
    },
    "valuation": {
        "model": "none",
        "model_id": "none",
        "max_tokens": 0,
        "est_input_tokens": 0,
        "api_calls": 0,  # pure Python — no API calls
        "status": "built",
        "description": "Draft bible valuation pass (bid ceilings, tiers, value gap)",
    },
}

PIPELINE_ORDER = [
    "team_systems",
    "roster_changes",
    "player_profiles",
    "injury_risk",
    "schedule",
    "beat_reporter",
    "valuation",
]

# Cost per million tokens
_RATES = {
    "haiku":  {"input": 0.80, "output": 4.00},
    "sonnet": {"input": 3.00, "output": 15.00},
}


def _estimate_cost(spec: dict, calls: int) -> float | None:
    if calls == 0:
        return None
    rates = _RATES[spec["model"]]
    input_cost  = spec["est_input_tokens"] * calls * rates["input"]  / 1_000_000
    output_cost = spec["max_tokens"]       * calls * rates["output"] / 1_000_000
    return input_cost + output_cost


# ---------------------------------------------------------------------------
# Dry-run output
# ---------------------------------------------------------------------------

def print_dry_run(agents: list[str], single_team: bool) -> None:
    scope_calls = 1 if single_team else 32
    print("\n=== Dry-Run Cost Estimate ===")
    print(f"  Scope : {'single team' if single_team else 'all 32 teams'}\n")

    fmt = "{:<22} {:<8} {:>7} {:>12} {:>12}  {}"
    print(fmt.format("Agent", "Model", "Calls", "Max tokens", "Est. cost", "Notes"))
    print("-" * 80)

    total = 0.0
    for name in agents:
        spec = AGENT_SPECS[name]
        if spec["api_calls"] is None:
            calls_str = "variable"
            cost_str  = "variable"
        elif spec["api_calls"] == 0:
            calls_str = "0"
            cost_str  = "$0.0000"
        else:
            calls = scope_calls
            cost  = _estimate_cost(spec, calls)
            calls_str = str(calls)
            cost_str  = f"${cost:.4f}"
            total += cost

        tag = "" if spec["status"] == "built" else "[NOT BUILT YET]"
        print(fmt.format(name, spec["model"], calls_str, spec["max_tokens"], cost_str, tag))

    print("-" * 80)
    print(fmt.format("TOTAL (built, fixed-call)", "", "", "", f"${total:.4f}", ""))

    not_built = [n for n in agents if AGENT_SPECS[n]["status"] == "not_built"]
    if not_built:
        print(f"\n  NOTE: {len(not_built)} agent(s) not yet built and will be skipped in a real run:")
        for n in not_built:
            print(f"       - {n}: {AGENT_SPECS[n]['description']}")
    print()


# ---------------------------------------------------------------------------
# Seed step
# ---------------------------------------------------------------------------

def run_seed() -> None:
    print("[seed] Seeding players table ...")
    result = subprocess.run(
        [sys.executable, "scripts/seed_nfl_data.py"],
    )
    if result.returncode != 0:
        print("[seed] FAILED — aborting.")
        sys.exit(1)
    print("[seed] Done.\n")


# ---------------------------------------------------------------------------
# Agent dispatch
# ---------------------------------------------------------------------------

async def run_agent(name: str, teams: list[str] | None) -> None:
    spec = AGENT_SPECS[name]
    if spec["status"] == "not_built":
        print(f"[{name}] SKIPPED — not built yet.")
        return

    t0 = time.monotonic()
    print(f"[{name}] Starting ({len(teams) if teams else 32} team(s)) ...")

    if name == "team_systems":
        from backend.agents.team_systems import TeamSystemsAgent, NFL_TEAMS
        agent = TeamSystemsAgent(dry_run=False)
        for team in (teams or NFL_TEAMS):
            await agent.run_for_team(team)

    elif name == "roster_changes":
        from backend.agents.roster_changes import RosterChangesAgent
        from backend.agents.team_systems import NFL_TEAMS
        agent = RosterChangesAgent(dry_run=False)
        if teams:
            for team in teams:
                await agent.run_for_team(team)
        else:
            await agent.run_all_teams()  # pre-loads OTC transactions before team loop

    elif name == "player_profiles":
        from backend.agents.player_profiles import PlayerProfilesAgent
        from backend.agents.team_systems import NFL_TEAMS
        agent = PlayerProfilesAgent(dry_run=False)
        if teams:
            for team in teams:
                await agent.run_for_team(team)
        else:
            await agent.run_all_teams()

    elif name == "injury_risk":
        from backend.agents.injury_risk import InjuryRiskAgent
        agent = InjuryRiskAgent(dry_run=False)
        if teams:
            for team in teams:
                await agent.run_for_team(team)
        else:
            await agent.run_all_teams()

    elif name == "schedule":
        from backend.agents.schedule import ScheduleAgent
        agent = ScheduleAgent(dry_run=False)
        if teams:
            for team in teams:
                await agent.run_for_team(team)
        else:
            await agent.run_all_teams()

    elif name == "beat_reporter":
        from backend.agents.beat_reporter import BeatReporterAgent
        agent = BeatReporterAgent(dry_run=False)
        # Beat reporter is not team-batched — ignores --team flag, runs all feeds
        signals = await agent.run()
        print(f"[{name}] {signals} new signal(s) written.")

    elif name == "valuation":
        from backend.engines.valuation import run_valuation_pass
        result = await run_valuation_pass()
        print(
            f"[{name}] {result['updated']} player(s) updated, "
            f"{result['skipped']} skipped "
            f"(analysis_year={result['analysis_year']})."
        )

    elapsed = time.monotonic() - t0
    print(f"[{name}] Done in {elapsed:.1f}s.\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the pre-draft AI pipeline")
    parser.add_argument(
        "--agent",
        default="all",
        metavar="NAME",
        help="Agent to run: all | team_systems | roster_changes | player_profiles | injury_risk | schedule | beat_reporter | valuation",
    )
    parser.add_argument(
        "--team",
        default=None,
        metavar="ABBR",
        help="Run for one team only (e.g. --team LAC). Omit for all 32.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print cost estimate only — no API calls, no DB writes",
    )
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="Skip re-seeding the players table (assume it is already populated)",
    )
    args = parser.parse_args()

    agents = PIPELINE_ORDER if args.agent == "all" else [args.agent]
    if args.agent != "all" and args.agent not in AGENT_SPECS:
        print(f"Unknown agent '{args.agent}'. Choose from: {', '.join(PIPELINE_ORDER)}")
        sys.exit(1)

    team_filter = args.team.upper() if args.team else None

    if args.dry_run:
        print_dry_run(agents, single_team=team_filter is not None)
        return

    # ---- Real run ----
    print(f"\n=== Pre-Draft Pipeline ===")
    print(f"  Agents : {', '.join(agents)}")
    print(f"  Scope  : {team_filter or 'all 32 teams'}")
    print()

    if not args.skip_seed:
        run_seed()

    teams = [team_filter] if team_filter else None
    for name in agents:
        await run_agent(name, teams)

    print("=== Pipeline complete ===\n")


if __name__ == "__main__":
    asyncio.run(main())
