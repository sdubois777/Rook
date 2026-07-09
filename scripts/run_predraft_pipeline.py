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
        "model": "mixed",
        "model_id": "claude-haiku-4-5-20251001",
        "max_tokens": 4000,
        "est_input_tokens": 1500,
        "api_calls": 120,
        "status": "built",
        "description": "Player projections — Haiku batch + Sonnet for complex players",
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
    "valuation_agent": {
        "model": "mixed",
        "model_id": "claude-sonnet-4-6",
        "max_tokens": 600,
        "est_input_tokens": 800,
        "api_calls": 60,
        "status": "built",
        "description": "AI ceiling calibration (confidence ranges, auction notes, flags)",
    },
}

PIPELINE_ORDER = [
    "team_systems",
    "roster_changes",
    "injury_risk",
    "schedule",
    "beat_reporter",
    "player_profiles",   # runs LAST — synthesizes all upstream agent outputs
    "kicker_baseline",   # dedicated K prior (offense profiler is skill-only)
    "defense_baseline",  # dedicated DST prior (crude historical, team-keyed)
    "valuation",
    "valuation_agent",   # AI ceiling calibration — runs after math valuation
    "availability",      # LAST: deterministic games-missed availability discount
]

# Cost per million tokens
_RATES = {
    "haiku":  {"input": 0.80, "output": 4.00},
    "sonnet": {"input": 3.00, "output": 15.00},
}


def _estimate_cost(spec: dict, calls: int) -> float | None:
    if calls == 0:
        return None
    model = spec["model"]
    if model == "mixed":
        # Estimate: 32 haiku batch + remaining sonnet individual
        haiku_calls = min(32, calls)
        sonnet_calls = max(0, calls - 32)
        h = haiku_calls * (
            spec["est_input_tokens"] * _RATES["haiku"]["input"]
            + spec["max_tokens"] * _RATES["haiku"]["output"]
        ) / 1_000_000
        s = sonnet_calls * (
            spec["est_input_tokens"] * _RATES["sonnet"]["input"]
            + 800 * _RATES["sonnet"]["output"]  # 800 max_tokens for Sonnet per-player
        ) / 1_000_000
        return h + s
    if model == "none":
        return 0.0
    rates = _RATES[model]
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

async def run_agent(name: str, teams: list[str] | None, force: bool = False, warehouse=None) -> None:
    spec = AGENT_SPECS[name]
    if spec["status"] == "not_built":
        print(f"[{name}] SKIPPED — not built yet.")
        return

    t0 = time.monotonic()
    print(f"[{name}] Starting ({len(teams) if teams else 32} team(s)) ...")

    if name == "team_systems":
        from backend.agents.team_systems import TeamSystemsAgent, NFL_TEAMS
        agent = TeamSystemsAgent(dry_run=False, warehouse=warehouse)
        if teams:
            for team in teams:
                await agent.run_for_team(team)
        else:
            await agent.run_all_teams(warehouse=warehouse)

    elif name == "roster_changes":
        from backend.agents.roster_changes import RosterChangesAgent
        agent = RosterChangesAgent(dry_run=False, warehouse=warehouse)
        if teams:
            for team in teams:
                await agent.run_for_team(team)
        else:
            await agent.run_all_teams(warehouse=warehouse)

    elif name == "player_profiles":
        from backend.agents.player_profiles import PlayerProfilesAgent
        agent = PlayerProfilesAgent(dry_run=False, warehouse=warehouse)
        if teams:
            for team in teams:
                await agent.run_for_team(team, force=force)
        else:
            await agent.run_all_teams(warehouse=warehouse, force=force)

    elif name == "injury_risk":
        from backend.agents.injury_risk import InjuryRiskAgent
        agent = InjuryRiskAgent(dry_run=False, warehouse=warehouse)
        if teams:
            for team in teams:
                await agent.run_for_team(team)
        else:
            await agent.run_all_teams(warehouse=warehouse)

    elif name == "schedule":
        from backend.agents.schedule import ScheduleAgent
        agent = ScheduleAgent(dry_run=False, warehouse=warehouse)
        if teams:
            for team in teams:
                await agent.run_for_team(team)
        else:
            await agent.run_all_teams(warehouse=warehouse)

    elif name == "beat_reporter":
        from backend.agents.beat_reporter import BeatReporterAgent
        agent = BeatReporterAgent(dry_run=False)
        # Beat reporter is not team-batched — ignores --team flag, runs all feeds
        signals = await agent.run()
        print(f"[{name}] {signals} new signal(s) written.")

    elif name == "kicker_baseline":
        # Dedicated preseason KICKER prior — writes clean_season_baseline.ppr_points
        # for K rows (the offense profiler is skill-only, so kickers are otherwise
        # priorless). Pure data step, no Sonnet. Own DB session.
        from backend.database import AsyncSessionLocal
        from backend.services.kicker_baseline import write_kicker_baselines
        async with AsyncSessionLocal() as _db:
            result = await write_kicker_baselines(_db)
        print(
            f"[{name}] {result['written']} kicker profile(s): "
            f"{result['historical']} historical, {result['rookie_default']} rookie-default, "
            f"{result['vet_default']} veteran-default (seasons={result['seasons']})."
        )

    elif name == "defense_baseline":
        # Dedicated preseason DEFENSE (DST) prior — writes clean_season_baseline
        # .ppr_points for team-unit DEF rows (crude historical, not a projection).
        # Pure data step, no Sonnet. Own DB session.
        from backend.database import AsyncSessionLocal
        from backend.services.defense_baseline import write_defense_baselines
        async with AsyncSessionLocal() as _db:
            result = await write_defense_baselines(_db)
        print(
            f"[{name}] {result['written']} defense profile(s): "
            f"{result['historical']} historical, {result['default_used']} default "
            f"(seasons={result['seasons']})."
        )

    elif name == "valuation":
        from backend.engines.valuation import run_valuation_pass
        result = await run_valuation_pass()
        print(
            f"[{name}] {result['updated']} player(s) updated, "
            f"{result['skipped']} skipped "
            f"(analysis_year={result['analysis_year']})."
        )

    elif name == "valuation_agent":
        from backend.agents.valuation_agent import ValuationAgent
        agent = ValuationAgent(dry_run=False)
        result = await agent.run_all()
        print(
            f"[{name}] {result['processed']} player(s) processed, "
            f"{result['skipped']} skipped."
        )

    elif name == "availability":
        # Deterministic pre-draft availability discount (games-missed proration for a
        # known multi-week absence). No Sonnet. Own DB session. Runs LAST.
        from backend.database import AsyncSessionLocal
        from backend.engines.availability_pass import apply_availability_discounts
        async with AsyncSessionLocal() as _db:
            result = await apply_availability_discounts(_db)
        print(
            f"[{name}] {result['discounted']} player(s) discounted for a known absence "
            f"(of {result['total']}), {result['updated']} rows updated."
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
        help="Agent to run: all | team_systems | roster_changes | player_profiles | injury_risk | schedule | beat_reporter | valuation | valuation_agent",
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force regeneration of all profiles, bypassing cache invalidation",
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

    # Sync rosters from Sleeper — must run after seed to fix team assignments
    # nfl_data_py seed data has stale teams; Sleeper has current rosters
    print("[sync_rosters] Syncing player rosters from Sleeper...")
    sync_result = subprocess.run(
        [sys.executable, "scripts/sync_rosters.py"],
    )
    if sync_result.returncode != 0:
        print("[sync_rosters] WARNING — sync failed, continuing with seed data.")
    print()

    # Sync FantasyPros ADP (snake-draft support) — populates adp_fantasypros
    # before the agent phases. Independent of the agents; a failure is non-fatal.
    print("[sync_adp] Syncing ADP from FantasyPros...")
    adp_result = subprocess.run(
        [sys.executable, "scripts/sync_adp.py"],
    )
    if adp_result.returncode != 0:
        print("[sync_adp] WARNING — ADP sync failed, continuing without ADP.")
    print()

    # Build warehouse once — all agents read from this shared data store
    from backend.integrations.nfl_data import NflDataWarehouse, populate_gsis_from_depth_charts
    print("[warehouse] Building NflDataWarehouse (one-time data load)...")
    t0 = time.monotonic()
    warehouse = NflDataWarehouse.build()
    summary = warehouse.summary()
    print(f"[warehouse] Built in {time.monotonic() - t0:.1f}s — {summary}")

    # Populate gsis_id for players that don't have it yet
    gsis_count = await populate_gsis_from_depth_charts(warehouse)
    if gsis_count:
        print(f"[gsis_id] Populated {gsis_count} players from depth charts")
    print()

    teams = [team_filter] if team_filter else None

    # Pipeline dependency phases — independent agents run in parallel
    _PHASES = [
        ["team_systems"],                              # Phase 1: no deps
        ["roster_changes"],                            # Phase 2: needs team_systems
        ["injury_risk", "schedule", "beat_reporter"],  # Phase 3: independent, parallel
        ["player_profiles"],                           # Phase 4: needs all above
        ["kicker_baseline"],                           # Phase 4b: dedicated K prior
        ["defense_baseline"],                          # Phase 4c: dedicated DST prior
        ["valuation"],                                 # Phase 5: needs profiles
        ["valuation_agent"],                           # Phase 6: needs valuation
        ["availability"],                              # Phase 7: LAST — availability discount
    ]

    for phase in _PHASES:
        phase_agents = [a for a in phase if a in agents]
        if not phase_agents:
            continue
        if len(phase_agents) == 1:
            await run_agent(phase_agents[0], teams, force=args.force, warehouse=warehouse)
        else:
            # Run independent agents in parallel
            await asyncio.gather(*(
                run_agent(a, teams, force=args.force, warehouse=warehouse)
                for a in phase_agents
            ))

    print("=== Pipeline complete ===\n")


if __name__ == "__main__":
    asyncio.run(main())
