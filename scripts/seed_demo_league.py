"""
TEST-ONLY demo-league seeder CLI — FLAG-GATED scaffolding (teardown: slice 6).

Constructs the demo trade league by rostering REAL 2025 players (see
DEMO_ROSTERS) onto demo teams and running them through the in-season value
engine, so you can eyeball the verdicts before the agents exist. Roster
construction + a pinned demo week only — it reads real weekly data, never writes
fabricated weekly stats.

Run (PowerShell):
    $env:TRADE_DEMO_MODE = "true"; uv run python scripts/seed_demo_league.py

Refuses to run unless TRADE_DEMO_MODE is set. The reusable logic lives in
backend/services/trade/trade_demo_source.py; this file is just the entrypoint.
Teardown deletes both, plus the TRADE_DEMO_MODE branches and the demo tests.
"""
from __future__ import annotations

import asyncio
import sys


async def _main() -> int:
    # The engine's `why` text uses unicode (→, —); keep the demo CLI printable
    # on a Windows cp1252 console.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from backend.database import AsyncSessionLocal
    from backend.services.trade.trade_demo_source import (
        DEMO_CURRENT_WEEK,
        DEMO_SEASON,
        seed_demo_league,
        trade_demo_enabled,
    )
    from backend.services.trade.value_engine import evaluate_league

    if not trade_demo_enabled():
        print("TRADE_DEMO_MODE is not set — refusing to seed the demo league.")
        return 1

    async with AsyncSessionLocal() as db:
        source = await seed_demo_league(db)
    state = source.get_league_state()
    values = evaluate_league(state, source.weekly_usage, priors=source.priors)

    print(f"Demo league — season {DEMO_SEASON}, week {DEMO_CURRENT_WEEK}")
    for team in state.teams:
        tag = " (you)" if team.is_me else ""
        print(f"\n== {team.team_name}{tag} ==")
        for rp in team.roster:
            v = values.get(rp.canonical_player_id)
            if v is None:
                print(f"  {rp.name:22s} {rp.position:3s}  (no value)")
                continue
            flag = "BUY " if v.buy_low else ("SELL" if v.sell_high else "   -")
            print(
                f"  {rp.name:22s} {rp.position:3s}  fv={v.forward_value:5.1f} "
                f"{v.value_trend.value:7s} {v.confidence.value:12s} {flag}  {v.why}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
