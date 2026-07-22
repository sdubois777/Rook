#!/usr/bin/env python
"""Phase B — null-anchor draftable-rookie fix (DEV ONLY, branch fix/null-anchor-draftable-rookies).

Disposition 1 of 2: Hunter + Evans get a MARKET-FREE forward projection so their
`projected_ppr_season` is populated → the valuation pass values them → real anchor.

Method — capture-run-restore, so ONLY these two rows move and the rest of the board is
byte-identical:
  1. capture every draftable player's stored value-fields
  2. write the market-free projection into Hunter/Evans profiles (projected_ppr_season,
     profile_source='sonnet_projection'); mvl was nulled at projection time (option C)
  3. run the REAL run_valuation_pass() (values everyone with H/E now in their pools)
  4. restore every draftable player EXCEPT Hunter/Evans to the captured values
     (reverts the pool-dilution the pass applied to others → byte-unchanged)

Projections are the MEDIAN of 3x market-free (mvl=None) Sonnet veteran runs (reconciled
vs 2025 usage): Hunter 178.0 (45tgt/28rec/7g → WR3 pace); Evans 52.0 (25tgt/19rec/17g).

Idempotent-ish: re-running recomputes the same projections; capture is taken fresh each run.
Dev guard: refuses to run unless DATABASE_URL points at the dev host.
"""
from __future__ import annotations
import asyncio
import logging
from sqlalchemy import select

from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerProfile
from backend.engines.valuation import run_valuation_pass, DRAFTABLE_POSITIONS

logging.disable(logging.CRITICAL)

PROJECTIONS = {"Travis Hunter": 178.0, "Mitchell Evans": 52.0}

VALUE_FIELDS = [
    "tier", "baseline_value", "ceiling_value", "floor_value", "risk_adjusted_value",
    "recommended_bid_ceiling", "let_go_threshold", "elite_anchor_weight",
    "positional_scarcity_modifier", "value_gap", "value_gap_signal", "data_confidence",
]
_KDEF = ("K", "DEF")


def _assert_dev() -> None:
    if "5433" not in settings.database_url and "localhost" not in settings.database_url:
        raise SystemExit(f"REFUSING: DATABASE_URL is not dev: {settings.database_url}")


async def main() -> None:
    _assert_dev()

    # ---- resolve target ids by name ----
    async with AsyncSessionLocal() as s:
        targets: dict[str, Player] = {}
        for name in PROJECTIONS:
            p = (await s.execute(select(Player).where(Player.name == name,
                 Player.recommended_bid_ceiling.is_(None)))).scalars().first()
            if p is None:
                raise SystemExit(f"target {name!r} not found with null anchor")
            targets[name] = p
        target_ids = {p.id for p in targets.values()}
        print("targets:", {n: str(p.id)[:8] for n, p in targets.items()})

    # ---- 1. capture every draftable/KDEF player's stored value-fields ----
    async with AsyncSessionLocal() as s:
        allp = (await s.execute(select(Player))).scalars().all()
        capture = {
            p.id: {f: getattr(p, f) for f in VALUE_FIELDS}
            for p in allp
            if p.position in DRAFTABLE_POSITIONS or p.position in _KDEF
        }
    print(f"captured value-fields for {len(capture)} players")

    # ---- 2. write market-free projection into Hunter/Evans profiles ----
    async with AsyncSessionLocal() as s:
        for name, proj in PROJECTIONS.items():
            pid = targets[name].id
            prof = (await s.execute(select(PlayerProfile).where(
                PlayerProfile.player_id == pid))).scalar_one()
            cb = dict(prof.clean_season_baseline or {})
            cb["projected_ppr_season"] = round(float(proj), 1)
            cb["_null_anchor_fix"] = "market-free median(3x, mvl=None) projection"
            prof.clean_season_baseline = cb
            prof.profile_source = "sonnet_projection"
            s.add(prof)
        await s.commit()
    print("wrote projected_ppr_season -> profiles (sonnet_projection)")

    # ---- 3. run the REAL valuation pass ----
    res = await run_valuation_pass()
    print(f"valuation pass: processed={res.get('processed')} updated={res.get('updated')}")

    # ---- 4. restore every draftable player EXCEPT the two targets ----
    async with AsyncSessionLocal() as s:
        allp = (await s.execute(select(Player))).scalars().all()
        restored = 0
        for p in allp:
            if p.id in target_ids or p.id not in capture:
                continue
            changed = False
            for f, v in capture[p.id].items():
                if getattr(p, f) != v:
                    setattr(p, f, v)
                    changed = True
            if changed:
                s.add(p)
                restored += 1
        await s.commit()
    print(f"restored {restored} non-target players to pre-pass values (byte-unchanged)")

    # ---- 5. report Hunter/Evans after ----
    async with AsyncSessionLocal() as s:
        for name in PROJECTIONS:
            p = (await s.execute(select(Player).where(Player.name == name))).scalars().first()
            print(f"  {name:16s} {p.position} tier={p.tier} baseline=${p.baseline_value} "
                  f"anchor(ceil)=${p.recommended_bid_ceiling} aic={p.ai_bid_ceiling}")


if __name__ == "__main__":
    asyncio.run(main())
