#!/usr/bin/env python
"""Evidence: market (`market_value_league`) is immaterial to the player_profiles
veteran-Sonnet projection — magnitude on Travis Hunter is inside sampling noise.

Motivation: the null-anchor fix routes thin-history rookies to the veteran Sonnet
projection. That path serializes the whole player dict (incl. `market_value_league`)
into the prompt (player_profiles.py ~L1669 -> L2059). Option C nulls mvl for the fixed
set for clean provenance; this script shows it changes ~nothing anyway, so (A) accept
and (C) strip converge on the same value.

Method: build Hunter's real JAX context, run the veteran Sonnet projection 3x with
mvl=7.00 (as-is) and 3x with mvl=None (stripped). Report projected_ppr_points spread
per variant + the market delta vs the within-variant noise. In-memory, no DB writes,
no cache (direct client). Standing evidence alongside the rejected-fix scripts
(reshape_phase1_baseline.py, market_free_dryrun.py, ...).

    .venv/Scripts/python.exe scripts/hunter_market_immateriality.py
"""
from __future__ import annotations
import warnings, logging, json, statistics
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import asyncio

from backend.integrations.nfl_data import NflDataWarehouse
from backend.agents.player_profiles import PlayerProfilesAgent, SONNET_SYSTEM_PROMPT
from backend.agents.base_agent import parse_json_output, SONNET

TEAM, NAME, RUNS = "JAX", "Travis Hunter", 3


async def main() -> None:
    wh = NflDataWarehouse.build()
    ag = PlayerProfilesAgent(dry_run=False, warehouse=wh)
    ctx = await ag._build_team_context(TEAM)
    base = next((p for p in ctx["players"] if p["name"] == NAME), None)
    if base is None:
        raise SystemExit(f"{NAME} not in {TEAM} context")
    shared = "TEAM CONTEXT (shared by all players below):\n" + json.dumps(
        {"team": TEAM, "analysis_year": ctx["analysis_year"], "team_system": ctx["team_system"]},
        sort_keys=True, default=str)
    system = shared + "\n\n" + SONNET_SYSTEM_PROMPT

    async def project(mvl) -> float | None:
        p = dict(base); p["market_value_league"] = mvl
        user = (f"Project PPR for {NAME} ({TEAM}) using the shared TEAM CONTEXT above "
                f"and this player data:\n\n{json.dumps({'player': p}, default=str)}")
        r = await ag._client.messages.create(model=SONNET, max_tokens=800, system=system,
                                             messages=[{"role": "user", "content": user}])
        prof = parse_json_output(r.content[0].text)
        if isinstance(prof, list) and prof:
            prof = prof[0]
        v = prof.get("projected_ppr_points") if isinstance(prof, dict) else None
        return float(v) if v else None

    results = {}
    for label, mvl in (("mvl=7.00 (as-is)", 7.00), ("mvl=None (stripped)", None)):
        vals = [await project(mvl) for _ in range(RUNS)]
        vals = [v for v in vals if v is not None]
        results[label] = vals
        print(f"{label:22s}: proj_ppr={vals} mean={statistics.mean(vals):.1f} "
              f"spread={max(vals) - min(vals):.1f}")

    m7 = statistics.mean(results["mvl=7.00 (as-is)"])
    mn = statistics.mean(results["mvl=None (stripped)"])
    noise = max(max(v) - min(v) for v in results.values())
    print(f"\nmarket delta (mean) = {abs(m7 - mn):.1f} PPR ; within-variant noise = {noise:.1f} PPR")
    print("VERDICT:", "immaterial (delta <= noise)" if abs(m7 - mn) <= noise
          else "MATERIAL — investigate")


if __name__ == "__main__":
    asyncio.run(main())
