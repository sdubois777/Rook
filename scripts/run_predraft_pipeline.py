"""
scripts/run_predraft_pipeline.py

Runs the pre-draft AI agent pipeline in order.

INCREMENTAL BY DEFAULT (the cost model — measured $10.82/full-sweep):
  * Default runs are DIRTY-ONLY: player_profiles regenerates only players whose
    MATERIAL inputs changed (value-delta fingerprint — injury status, depth,
    dependency flags, beat signals, team system; see profile_needs_refresh),
    and every agent's identical inputs hit agent_cache at zero tokens. A
    realistic in-season cycle re-profiles a handful of players (~$0.05-2),
    not all ~660 ($10.82).
  * FULL SWEEP is explicit: --full-sweep (alias --force) bypasses the dirty
    test. Operating model: full sweeps belong to PRE-DRAFT (profiles rebuild
    off completed-season baselines); IN-SEASON is daily news ingestion +
    event-triggered dirty refreshes only — no scheduled full sweeps.
  * Prompt caching (1h TTL) rides on every call automatically via BaseAgent.

Usage:
    uv run python scripts/run_predraft_pipeline.py --dry-run
    uv run python scripts/run_predraft_pipeline.py --agent all              # dirty-only
    uv run python scripts/run_predraft_pipeline.py --agent all --full-sweep # pre-draft rebuild
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
    "kicker_baseline": {
        "model": "none",
        "model_id": "none",
        "max_tokens": 0,
        "est_input_tokens": 0,
        "api_calls": 0,  # pure data step — no API calls
        "status": "built",
        "description": "Dedicated preseason kicker prior (clean_season_baseline for K rows)",
    },
    "defense_baseline": {
        "model": "none",
        "model_id": "none",
        "max_tokens": 0,
        "est_input_tokens": 0,
        "api_calls": 0,  # pure data step — no API calls
        "status": "built",
        "description": "Dedicated preseason DST prior (crude historical, team-keyed)",
    },
    "team_metrics": {
        "model": "none",
        "model_id": "none",
        "max_tokens": 0,
        "est_input_tokens": 0,
        "api_calls": 0,  # deterministic Teams-page fields — no API calls
        "status": "built",
        "description": "Deterministic Teams-page fields (scheme, pass-pro, qb_tier)",
    },
    "team_notes": {
        "model": "haiku",
        "model_id": "claude-haiku-4-5-20251001",
        "max_tokens": 180,
        "est_input_tokens": 400,
        "api_calls": 32,
        "status": "built",
        "description": "Regenerate Teams-page system-notes prose from stored stats (Haiku)",
    },
    "availability": {
        "model": "none",
        "model_id": "none",
        "max_tokens": 0,
        "est_input_tokens": 0,
        "api_calls": 0,  # deterministic games-missed discount — no API calls
        "status": "built",
        "description": "Deterministic games-missed availability discount (runs last)",
    },
    "format_market": {
        "model": "none",
        "model_id": "none",
        "max_tokens": 0,
        "est_input_tokens": 0,
        "api_calls": 0,  # Playwright scrape, no AI calls
        "status": "built",
        "description": "Per-format ADP + auction re-scrape into player_format_values (every run)",
    },
}

# Pipeline dependency phases — the SINGLE source of truth for execution order.
# Inner lists run in parallel (independent agents); the outer list is sequential.
PHASES = [
    ["team_systems"],                              # Phase 1: identity + inputs (rows, QB id, sack_rate, rookie flag) — NO grades
    ["team_metrics"],                              # Phase 1b: DETERMINISTIC grades + composite — the SOLE grade owner
    ["roster_changes"],                            # Phase 2: needs team_systems + the deterministic grades above
    ["injury_risk", "schedule", "beat_reporter"],  # Phase 3: independent, parallel
    ["player_profiles"],                           # Phase 4: needs all above
    ["kicker_baseline"],                           # Phase 4b: dedicated K prior
    ["defense_baseline"],                          # Phase 4c: dedicated DST prior
    ["valuation"],                                 # Phase 5: needs profiles
    ["valuation_agent"],                           # Phase 6: needs valuation
    ["format_market"],                             # Phase 6b: re-scrape per-format ADP + auction (every run)
    ["team_notes"],                                # Phase 6c: grounded NARRATOR — narrate from the real grades/stats
    ["availability"],                              # Phase 7: LAST — availability discount
]

# DERIVED, never hand-maintained. PIPELINE_ORDER is the membership set for `--agent all`
# and the row order of the dry-run table.
#
# It used to be a separate hand-written list, and it had DRIFTED: it placed team_metrics
# 12th and team_notes 13th, while PHASES runs them at 1b and 6c. Two ways that bites --
# the dry-run advertised an execution order the pipeline does not follow, and because the
# phase loop filters each phase by `a in agents`, any stage present in PHASES but missing
# from this list would be SILENTLY SKIPPED under `--agent all` with no error. Deriving it
# makes both classes of divergence impossible.
PIPELINE_ORDER = [agent for phase in PHASES for agent in phase]

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
            # force MUST be threaded here. run_all_teams passes skip_if_fresh=not force
            # (roster_changes.py:1617), so without it every team analyzed inside
            # ROSTER_CHANGES_STALENESS_DAYS (7) is skipped even under --full-sweep, whose
            # own docstring calls force "required for a deliberate full-sweep regen".
            #
            # The silent-corruption case this fixes: replace_team() wipes a team's rows
            # before writing (roster_changes.py:1790), so a deliberate regen against a
            # cleared player_dependencies table would skip all 32 teams and repopulate
            # NOTHING -- every player left with zero dependency flags, no error raised,
            # and a board that still looks plausible.
            #
            # roster_changes is the only one of these agents that takes force at all;
            # team_systems / injury_risk / schedule have no staleness skip and rely on
            # the agent_cache fingerprint, so omitting it there is correct.
            await agent.run_all_teams(warehouse=warehouse, force=force)

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
        # AS-OF RUNS SKIP THIS STAGE. It ingests LIVE RSS (ESPN, Rotowire, NFL.com) with
        # no season parameter, no date filter and no archive, so there is no way to fetch
        # the news as it stood on a past date — running it would inject present-day
        # reporting into a past-season board, and beat signals feed the projection prose.
        #
        # This is a KNOWN, DELIBERATE difference from a real board of that vintage: the
        # as-of board has no beat signals at all rather than the wrong ones. The
        # complementary half is in player_profiles._get_team_beat_signals, which bounds
        # its read by flagged_at — skipping the stage alone would still leak any signals
        # already sitting in the table.
        from backend.utils.seasons import asof_active, asof_date

        if asof_active():
            print(f"[{name}] SKIPPED — as-of run ({asof_date()}). Live RSS has no "
                  f"archive, so a past-dated board gets no beat signals rather than "
                  f"present-day ones.")
            return

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
        from backend.engines.valuation import (
            run_valuation_pass, write_format_value_sets, _load_prior_production,
        )
        # STEP 4 guard input — prior-season per-game production (pure data load, no AI).
        prior = _load_prior_production()
        result = await run_valuation_pass(prior_production=prior)
        print(
            f"[{name}] {result['updated']} player(s) updated, "
            f"{result['skipped']} skipped, "
            f"{len(result.get('displaced_suppressed', []))} displaced-guard suppression(s) "
            f"(analysis_year={result['analysis_year']})."
        )
        # Per-format (PPR/Half/Standard) value sets — reprices the same board via the
        # shared math and writes player_format_values (PPR row == the players table).
        fmt_result = await write_format_value_sets(prior_production=prior)
        print(f"[{name}] per-format value sets: {fmt_result['written']} rows "
              f"across {fmt_result['formats']} "
              f"({len(fmt_result.get('suppressed', []))} displaced-guard suppression(s)).")

    elif name == "valuation_agent":
        from backend.agents.valuation_agent import ValuationAgent
        agent = ValuationAgent(dry_run=False)
        result = await agent.run_all()
        print(
            f"[{name}] {result['processed']} player(s) processed, "
            f"{result['skipped']} skipped."
        )
        # POSITIONAL BUDGET — rail every position's ai_bid_ceiling onto its budget share.
        # MUST be here, not inside run_valuation_pass: that pass writes
        # recommended_bid_ceiling, which the agent above then overwrites, so enforcing
        # upstream leaves the agent free to undo it and never reaches the board. Runs
        # BEFORE reconcile_value_signals (so value_gap is computed against the final
        # ceiling) and BEFORE run_prose_for_format (which copies the format-invariant
        # QB/K/DEF ceilings out of the players table). Pure Python, no AI.
        from backend.engines.valuation import (
            enforce_ai_ceiling_budgets, enforce_format_ai_ceiling_budgets,
        )
        enf = await enforce_ai_ceiling_budgets()
        _before, _after = enf["before"], enf["after"]
        print(
            f"[{name}] positional budget enforced on {enf['updated']} ceiling(s): "
            f"${sum(_before.values()):.0f} -> ${sum(_after.values()):.0f} "
            f"against a ${enf['pool']:.0f} pool."
        )

        # STEP 5 (Phase 6) — DETERMINISTIC market-relative post-pass now that ai_bid_ceiling
        # is final and blind: recompute value_gap/signal AND derive value_assessment/
        # pay_up_flag/nomination_target_flag from the blind ceiling vs market. Pure DB, no AI.
        from backend.engines.valuation import reconcile_value_signals
        rec = await reconcile_value_signals()
        print(
            f"[{name}] reconciled value signals for {rec['updated']} player(s); "
            f"pay_up={rec['flag_counts']['pay_up']}, "
            f"nomination_target={rec['flag_counts']['nomination_target']}."
        )
        # Surface the signal basis actually used. A position showing 0 fell back to the
        # legacy dollar gap (too few priced players, or a non-positive price curve) —
        # worth noticing, because that position's signals are then the weaker basis.
        _curves = ", ".join(
            f"{pos}={n}" for pos, n in sorted(rec.get("price_curves", {}).items()) if pos
        )
        print(
            f"[{name}] price curves fitted: {_curves or 'none'}; "
            f"{rec.get('legacy_fallbacks', 0)} player(s) on the legacy dollar-gap basis."
        )
        # Per-format prose (G2): PPR copies the players-table narrative (byte-identical);
        # Half/Standard regenerate format-appropriate prose into player_format_values.
        copied = await agent.copy_ppr_prose_to_format_rows()
        print(f"[{name}] PPR prose copied to {copied} format rows.")
        for _fmt in ("half_ppr", "standard"):
            pr = await agent.run_prose_for_format(_fmt)
            print(f"[{name}] {_fmt} prose: {pr['processed']} processed, {pr['written']} written.")
        # ...and the same budget rail on the per-format ceilings the hybrid just wrote.
        # Targets come from _format_budget_shares, which holds QB's share fixed across
        # formats — so an enforced QB prices identically in every format, which is what
        # format-invariance actually means.
        fenf = await enforce_format_ai_ceiling_budgets()
        for _fmt, _s in sorted(fenf["formats"].items()):
            print(f"[{name}] {_fmt} budget: {_s['updated']}/{_s['rows']} ceiling(s) railed.")

    elif name == "format_market":
        # Per-format ADP (FantasyPros) + auction (DraftWizard, canonical flex roster)
        # re-scraped LIVE every run and written to player_format_values. NOT cached — the
        # inputs drift daily before draft day. Independent of the agents; failure is
        # non-fatal (leaves the prior per-format market rows in place). Own DB session.
        #
        # Skipped under an as-of clock for the same reason as sync_adp: the scrape is
        # current-season only, so it would write NEXT year's per-format market onto a
        # past-season board. The as-of market comes from _seed_asof_market() instead.
        from backend.utils.seasons import asof_active as _aa

        if _aa():
            print(f"[{name}] SKIPPED — as-of run. Live per-format market is current-season only.")
            return

        from backend.services.format_market_ingest import run_format_market_ingest_stage
        try:
            result = await run_format_market_ingest_stage()
        except Exception as exc:  # noqa: BLE001 — scrape failures must not abort the pipeline
            print(f"[{name}] WARNING — ingest failed ({exc}); prior market rows unchanged.")
        else:
            for _fmt, _s in result["formats"].items():
                print(
                    f"[{name}] {_fmt}: ADP {_s['adp_matched']}/{_s['adp_total']} matched, "
                    f"auction {_s['auction_matched']}/{_s['auction_total']} matched, "
                    f"{_s['rows']} rows."
                )
            print(
                f"[{name}] {result['rows_written']} rows written across "
                f"{len(result['formats'])} formats (roster {result['roster_shape']})."
            )

    elif name == "team_metrics":
        # Deterministic Teams-page fields (Teams rework slice 1): scheme from real
        # pass_rate (PBP), pass-protection grade from real sack_rate, qb_tier from real
        # cpoe (NGS) — replaces the LLM-overridden/compressed values. No Sonnet.
        from backend.database import AsyncSessionLocal
        from backend.engines.team_metrics import apply_team_deterministic_fields
        async with AsyncSessionLocal() as _db:
            result = await apply_team_deterministic_fields(_db)
        print(
            f"[{name}] {result['teams']} teams: scheme={result['scheme']} "
            f"pass_pro={result['pass_pro']} qb_tier={result['qb_tier']} "
            f"run_block={result['run_block']} personnel={result['personnel']} "
            f"red_zone={result['red_zone']} written (missing pass_rate={result['missing_pass_rate']}, "
            f"cpoe={result['missing_cpoe']}, stuff_rate={result['missing_runblock']})."
        )

    elif name == "team_notes":
        # Regenerate the Teams-page system-notes prose from the REAL stored stats +
        # widened-bell grades (narrate from real numbers, never invent). Haiku.
        from backend.database import AsyncSessionLocal
        from backend.agents.team_notes import regenerate_team_notes
        async with AsyncSessionLocal() as _db:
            result = await regenerate_team_notes(_db)
        print(f"[{name}] {result['written']} team note(s) regenerated from real stats, {result['failed']} failed.")

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
# Targeted refresh (PART 1/2) — scoped, event-driven
# ---------------------------------------------------------------------------

async def run_targeted_cli(args) -> None:
    """Resolve names → players, optionally DERIVE the affected set from an event,
    then run a scoped targeted refresh (dry-run aware)."""
    from backend.database import AsyncSessionLocal
    from backend.repositories.player_repo import PlayerRepository
    from backend.services.pipeline_triggers import (
        derive_affected_set, run_targeted_refresh,
    )

    async with AsyncSessionLocal() as db:
        repo = PlayerRepository(db)
        player_ids: set = set()
        event_type = "manual"

        if args.player:
            trigger = await repo.find_by_name_fuzzy(args.player)
            if trigger is None:
                print(f"Player not found: {args.player}")
                sys.exit(1)
            event_type = args.event or "injury"
            derived = await derive_affected_set(
                db, trigger, event_type, new_team=args.new_team
            )
            print(f"\n=== Affected set for {trigger.name} ({event_type}) — DERIVED ===")
            for entry in derived["affected"]:
                p = entry["player"]
                print(f"  • {p.name} ({p.team_abbr or 'FA'} {p.position or '?'})")
                for r in entry["reasons"]:
                    print(f"      - {r}")
            player_ids = {e["player"].id for e in derived["affected"]}
        else:
            for name in [n.strip() for n in args.players.split(",") if n.strip()]:
                p = await repo.find_by_name_fuzzy(name)
                if p is None:
                    print(f"  (skip) player not found: {name}")
                    continue
                player_ids.add(p.id)

    if not player_ids:
        print("No players resolved — nothing to refresh.")
        return

    report = await run_targeted_refresh(
        player_ids,
        event_type=event_type,
        dry_run=args.dry_run,
        respect_draft_window=not args.ignore_draft_window,
    )

    print(f"\n=== Targeted refresh {'(DRY RUN)' if args.dry_run else ''} ===")
    print(f"  Players touched : {report['n_players']}  ({', '.join(report['players_touched'])})")
    print(f"  Teams           : {', '.join(report['teams']) or '(none)'}")
    print(f"  Est. cost       : ${report['estimated_cost_usd']}  "
          f"vs full sweep ${report['full_sweep_cost_usd']}")
    if report.get("deferred"):
        print(f"  DEFERRED        : {report['reason']}  (draft window active)")
    elif not args.dry_run:
        print(f"  Profiles written: {report.get('profiles_written')}, "
              f"values: {report.get('values_updated')}, "
              f"ceilings: {report.get('ceilings_processed')}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _seed_asof_market() -> None:
    """Copy the as-of season's real auction prices into the market columns.

    Live FantasyPros is current-season only, so under an as-of run the market columns
    would otherwise hold NEXT year's consensus. market_value_historic holds what the
    league actually paid that season, which is both the correct market for the board and
    the series the backtest scores against.

    SOURCES, in order: the league's own auction (``league_auction_history``, which both
    the results importer and the league sync write, and which ``run_backtest`` also
    prefers), then ``market_value_historic``. The board and the scoring therefore agree
    on what "the market" was for that season.

    THE STALE-MARKET RESIDUAL IS NOW CLEARED, not left. An earlier version only
    overwrote the players the season had priced, so anyone absent from that auction kept
    whatever the previous real-time run left behind. On the 2025 board that was mild — 28
    skill players, all <= $15. On the 2024 rebuild it was not: ``market_value_historic``
    holds no 2024 rows at all, so the entire board kept 2026 consensus and priced Jaxon
    Smith-Njigba at $61 against his actual $1 and Rashee Rice at $43 against $12. Signals
    are computed against the market, so that is a confident WRONG signal on every player
    — the same class of error that voided an earlier backtest. Clearing first means an
    unpriced player gets no market-relative claim, which is honest.
    """
    from sqlalchemy import text as _text

    from backend.database import AsyncSessionLocal as _Session
    from backend.utils.seasons import get_current_season as _season

    season = _season()
    async with _Session() as s:
        # 1. CLEAR FIRST. Without this, any player the as-of season did not price keeps
        #    whatever the previous (real-time) run left behind, and his signals are then
        #    computed against a market from another year. Measured on the 2024 rebuild
        #    before this clear existed: the board carried 2026 consensus, pricing Jaxon
        #    Smith-Njigba at $61 against his actual $1 and Rashee Rice at $43 against $12.
        #    A wrong market is worse than no market — no market yields no signal, which is
        #    honest; a wrong one yields a confident wrong signal, which is what voided an
        #    earlier backtest run.
        await s.execute(_text(
            "UPDATE players SET market_value_fantasypros = NULL, market_value_league = NULL"
        ))

        # 2. The league's OWN auction first. league_auction_history is what the results
        #    importer and the league sync write, and run_backtest prefers it too, so the
        #    board and the scoring agree on what "the market" was.
        league = await s.execute(_text(
            "UPDATE players p SET market_value_fantasypros = h.price, "
            "       market_value_league = h.price "
            "FROM (SELECT player_id, avg(price) AS price FROM league_auction_history "
            "      WHERE season_year = :yr AND price > 0 AND player_id IS NOT NULL "
            "      GROUP BY player_id) h "
            "WHERE h.player_id = p.id"
        ), {"yr": season})

        # 3. Fall back to the season-keyed price reference for anyone still unpriced.
        historic = await s.execute(_text(
            "UPDATE players p SET market_value_fantasypros = h.price, "
            "       market_value_league = h.price "
            "FROM market_value_historic h "
            "WHERE h.player_id = p.id AND h.season_year = :yr AND h.price > 0 "
            "  AND p.market_value_fantasypros IS NULL"
        ), {"yr": season})
        await s.commit()

    total = league.rowcount + historic.rowcount
    print(
        f"[asof_market] {total} player(s) priced for {season} "
        f"({league.rowcount} from the league's own auction, "
        f"{historic.rowcount} from market_value_historic); "
        "every other player's market was CLEARED."
    )
    if total < 50:
        print(
            f"[asof_market] WARNING — only {total} priced players for {season}. "
            "Signals will be market-blind for most of the board, and a backtest of this "
            "season cannot be scored. Import that season's auction results first "
            "(scripts/import_auction_results.py)."
        )


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
        "--force", "--full-sweep",
        dest="force",
        action="store_true",
        help="FULL SWEEP (pre-draft): regenerate all profiles, bypassing the "
             "value-delta dirty test. Default runs are dirty-only (in-season).",
    )
    parser.add_argument(
        "--players",
        default=None,
        metavar="NAMES",
        help="TARGETED REFRESH: comma-separated player names to recompute (scoped, "
             "not the whole board). Reuses the dirty/cache machinery.",
    )
    parser.add_argument(
        "--player",
        default=None,
        metavar="NAME",
        help="TARGETED REFRESH from an EVENT: derive the affected set for one player "
             "+ --event, then refresh that set.",
    )
    parser.add_argument(
        "--event",
        default=None,
        metavar="TYPE",
        help="Event type for --player: injury | suspension | trade | drop_release | signing",
    )
    parser.add_argument(
        "--new-team",
        default=None,
        metavar="ABBR",
        help="Destination team for a --event trade/signing (crowds that position room).",
    )
    parser.add_argument(
        "--ignore-draft-window",
        action="store_true",
        help="Run the targeted refresh even if a draft window is active (operator override).",
    )
    args = parser.parse_args()

    # Refuse to run a real (writing) pipeline against prod unless deliberately
    # overridden. --dry-run makes no writes, so it is exempt. Covers BOTH the targeted
    # path below and the full/team path.
    if not args.dry_run:
        from backend.db_guard import guard_writes
        guard_writes("run_predraft_pipeline.py (writes players/profiles/valuations)")

    # --- TARGETED REFRESH mode (PART 1/2) — distinct from the full/team pipeline ---
    if args.players or args.player:
        await run_targeted_cli(args)
        return

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

    # Refresh players.espn_id / players.yahoo_id from Sleeper (primary) with
    # nflverse import_ids as fill. Runs here because seed + sync_rosters have just
    # inserted this season's new players, and those rows arrive with both columns
    # NULL — every rookie and mid-season signing would otherwise stay unresolvable
    # for ESPN and Sleeper league sync until someone ran the script by hand.
    #
    # Safe in the pipeline: it writes ONLY those two id columns. It never touches
    # market_value_*, ai_bid_ceiling or recommended_bid_ceiling, so it cannot move
    # a board value. It fills empty columns only — correcting an existing wrong
    # value needs the explicit --repair flag, which is deliberately NOT passed
    # here so an automatic run can never rewrite identity unattended.
    # A failure is non-fatal: stale ids degrade platform matching, they do not
    # invalidate the board.
    print("[platform_ids] Refreshing espn_id / yahoo_id...")
    ids_result = subprocess.run(
        [sys.executable, "scripts/backfill_platform_ids.py"],
    )
    if ids_result.returncode != 0:
        print("[platform_ids] WARNING — refresh failed; ESPN/Sleeper league sync "
              "may not resolve newly added players.")
    print()

    # Sync FantasyPros ADP (snake-draft support) — populates adp_fantasypros
    # before the agent phases. Independent of the agents; a failure is non-fatal.
    # Live FantasyPros scrape — CURRENT consensus, with no historical equivalent. Under
    # an as-of run it would put next year's market on a past-season board: measured, 2026
    # FP had Nico Collins at $31 and Saquon at $31 while the real 2025 auction paid $62
    # and $61. Signals compare our value against "the market" and are then scored against
    # the as-of season's price, so a mismatched market makes those metrics meaningless.
    # The as-of market comes from market_value_historic instead (see _seed_asof_market).
    from backend.utils.seasons import asof_active as _asof_active

    if _asof_active():
        print("[sync_adp] SKIPPED — as-of run. Live FantasyPros ADP is current-season only.")
    else:
        print("[sync_adp] Syncing ADP from FantasyPros...")
        adp_result = subprocess.run(
            [sys.executable, "scripts/sync_adp.py"],
        )
        if adp_result.returncode != 0:
            print("[sync_adp] WARNING — ADP sync failed, continuing without ADP.")
    print()

    # As-of market: the season's REAL auction, from market_value_historic. That is what
    # "the market" actually was on the as-of date, and it is the same series the backtest
    # scores against — so a "we are above market" signal and its scoring finally refer to
    # one market instead of two.
    if _asof_active():
        await _seed_asof_market()

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

    for phase in PHASES:
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
