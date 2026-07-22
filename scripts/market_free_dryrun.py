#!/usr/bin/env python
"""Market-free anchor dry-run: (a) current board vs (b) market-free pure pool-share.
READ-ONLY — recomputes the anchor in memory from stored baseline_value; makes NO DB writes.

    .venv/Scripts/python.exe scripts/market_free_dryrun.py

(a) = stored recommended_bid_ceiling (old: market blend + tier-1 scarcity).
(b) = compute_bid_ceiling(baseline_value, ...) under the NEW market-free pure-pool-share code,
      then capped at MAX_REALISTIC_BID (as run_valuation_pass does).
Reports per-position $ distribution + sum-to-budget, board↔market correlation for (a) and (b),
clamp reliance by name, and a top-50 + depth sample before/after.
"""
from __future__ import annotations
import warnings; warnings.filterwarnings("ignore")
import asyncio, statistics
from decimal import Decimal
from collections import defaultdict
from sqlalchemy import select
from backend.database import AsyncSessionLocal
from backend.models.player import Player
from backend.engines.valuation import (
    compute_bid_ceiling, MAX_REALISTIC_BID, POSITION_BUDGET_SHARE, LEAGUE_SKILL_DOLLAR_POOL,
)

POSITIONS = ["QB", "RB", "WR", "TE"]


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    dy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    return num / (dx * dy) if dx and dy else float("nan")


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for rank, i in enumerate(order):
            r[i] = rank
        return r
    return pearson(ranks(xs), ranks(ys))


async def main() -> None:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Player).where(
                Player.recommended_bid_ceiling.isnot(None),
                Player.baseline_value.isnot(None),
                Player.position.in_(POSITIONS),
            )
        )).scalars().all()

    recs = []
    for p in rows:
        pos = p.position
        cap = Decimal(str(MAX_REALISTIC_BID.get(pos, 80)))
        cur = float(p.recommended_bid_ceiling)                                   # (a)
        newv = float(min(compute_bid_ceiling(p.baseline_value, None, p.tier, pos), cap))  # (b)
        recs.append({
            "name": p.name, "pos": pos, "tier": p.tier,
            "a": cur, "b": newv, "cap": float(cap),
            "mkt": float(p.market_value_fantasypros) if p.market_value_fantasypros else None,
            "sv": float(p.baseline_value),
        })

    # Per-position $ distribution + sum-to-budget
    print("=== PER-POSITION $ (anchor = recommended_bid_ceiling) ===")
    print("  pos  n    budget   sum_(a)current   sum_(b)marketfree   delta")
    per = defaultdict(lambda: [0, 0.0, 0.0])
    for r in recs:
        d = per[r["pos"]]; d[0] += 1; d[1] += r["a"]; d[2] += r["b"]
    tot = [0, 0.0, 0.0]
    for pos in POSITIONS:
        n, a, b = per[pos]; tgt = round(POSITION_BUDGET_SHARE[pos] * LEAGUE_SKILL_DOLLAR_POOL)
        print(f"  {pos:3s} {n:4d}  {tgt:6d}   {a:10.0f}      {b:10.0f}     {b-a:+7.0f}")
        tot[0] += n; tot[1] += a; tot[2] += b
    print(f"  ALL {tot[0]:4d}  {round(LEAGUE_SKILL_DOLLAR_POOL):6d}   {tot[1]:10.0f}      {tot[2]:10.0f}     {tot[2]-tot[1]:+7.0f}")

    # Board <-> market correlation (players with an FP market)
    mk = [r for r in recs if r["mkt"] is not None and r["mkt"] > 0]
    print(f"\n=== BOARD <-> MARKET correlation (n={len(mk)} with FP market) ===")
    for metric, fn in (("pearson", pearson), ("spearman", spearman)):
        ca = fn([r["a"] for r in mk], [r["mkt"] for r in mk])
        cb = fn([r["b"] for r in mk], [r["mkt"] for r in mk])
        print(f"  {metric}: (a) current={ca:.3f}   (b) market-free={cb:.3f}   (drop {ca-cb:+.3f})")

    # Clamp reliance under (b)
    clamped = [r for r in recs if abs(r["b"] - r["cap"]) < 1e-6]
    print(f"\n=== CLAMP RELIANCE under (b): {len(clamped)} player(s) at MAX_REALISTIC_BID ===")
    for r in sorted(clamped, key=lambda r: -r["sv"]):
        print(f"    {r['name'][:22]:22s} {r['pos']} T{r['tier']}  sv=${r['sv']:.1f} -> capped ${r['cap']:.0f} "
              f"(uncapped sv=${r['sv']:.1f})")

    # Top 50 by current + a depth sample
    print("\n=== TOP 25 by (a) current: name pos tier  (a)->(b)  market ===")
    for r in sorted(recs, key=lambda r: -r["a"])[:25]:
        print(f"    {r['name'][:22]:22s} {r['pos']} T{str(r['tier']):2s}  ${r['a']:5.1f} -> ${r['b']:5.1f}  "
              f"(mkt ${r['mkt'] if r['mkt'] else 0:.0f})  d={r['b']-r['a']:+.1f}")
    print("\n=== DEPTH sample ($3-10 current): name pos tier (a)->(b) ===")
    depth = [r for r in recs if 3 <= r["a"] <= 10]
    for r in sorted(depth, key=lambda r: -r["a"])[:12]:
        print(f"    {r['name'][:22]:22s} {r['pos']} T{str(r['tier']):2s}  ${r['a']:5.1f} -> ${r['b']:5.1f}  d={r['b']-r['a']:+.1f}")


if __name__ == "__main__":
    asyncio.run(main())
