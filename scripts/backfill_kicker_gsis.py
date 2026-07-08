"""
scripts/backfill_kicker_gsis.py

Backfill ``players.gsis_id`` on KICKER rows from the nflverse id crosswalk,
matching on ``sleeper_id`` then ``sportradar_id`` (both ~100% populated on K
rows). Fixes the K/DEF-streaming gsis-join gap: kicker rows are Sleeper-native
(the K/DEF-arc ingest via ``fetch_sleeper_players``) and Sleeper fills gsis_id
for only ~1/3 of them, so ``build_weekly_kdef``'s gsis join can't populate the
DB gsis map for those rows. Filling gsis at the source lets them resolve
directly, without leaning on the runtime bridge (which is stale for newer
players).

SOURCE — ``nfl.import_ids()`` (via ``load_id_bridge``), NOT ``import_players()``.
Verified against real nflverse data (July 2026): ``import_players()`` exposes
ONLY ``gsis_id / esb_id / pfr_id / smart_id`` — it has NO ``sleeper_id`` or
``sportradar_id`` column, so it cannot be matched against Sleeper-native kicker
rows. ``import_ids()`` is the crosswalk that carries all four id families and is
already what the runtime bridge (``load_id_bridge``) wraps.

IDEMPOTENT: only fills rows where ``gsis_id IS NULL``; never overwrites an
existing gsis. A second run fills 0. Scoped to K (extend via ``POSITIONS``).

Note: a kicker absent from ``import_ids()`` entirely (very new UDFA) won't be
filled here — the ``build_weekly_kdef`` name+team fallback covers that case at
resolution time, and this backfill catches up once nflverse adds them.

Usage:
    uv run python scripts/backfill_kicker_gsis.py
    uv run python scripts/backfill_kicker_gsis.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root is on path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

# Scoped to kickers for slice 5b. Written so it could extend to other positions
# later (the residual-risk follow-up: Sleeper-native, null-gsis players who play)
# — do NOT broaden without a deliberate decision.
POSITIONS = ("K",)


async def backfill_kicker_gsis(dry_run: bool = False, db=None) -> dict:
    """Fill NULL gsis_id on kicker rows from the nflverse id crosswalk.

    Match priority per row: sleeper_id -> sportradar_id. Returns counts:
    ``candidates`` (null-gsis rows considered), ``filled``, ``still_null``
    (with identities), ``by_sleeper`` / ``by_sportradar``.
    """
    from sqlalchemy import select

    from backend.integrations.nfl_weekly import _norm_id, load_id_bridge
    from backend.models.player import Player

    bridge = load_id_bridge()
    # {normalized sleeper_id -> gsis}, {normalized sportradar_id -> gsis}
    sleeper_to_gsis: dict[str, str] = {}
    sportradar_to_gsis: dict[str, str] = {}
    for row in bridge.itertuples(index=False):
        gsis = _norm_id(getattr(row, "gsis_id", None))
        if not gsis:
            continue
        sl = _norm_id(getattr(row, "sleeper_id", None))
        sr = _norm_id(getattr(row, "sportradar_id", None))
        if sl:
            sleeper_to_gsis.setdefault(sl, gsis)
        if sr:
            sportradar_to_gsis.setdefault(sr, gsis)

    if db is None:
        from backend.database import AsyncSessionLocal
        session_ctx = AsyncSessionLocal()
        session = await session_ctx.__aenter__()
    else:
        session_ctx = None
        session = db

    filled = by_sleeper = by_sportradar = 0
    candidates = 0
    still_null: list[str] = []
    try:
        rows = (await session.execute(
            select(Player).where(
                Player.position.in_(POSITIONS), Player.gsis_id.is_(None)
            )
        )).scalars().all()
        candidates = len(rows)

        for p in rows:
            gsis = None
            src = None
            sl = _norm_id(p.sleeper_id)
            sr = _norm_id(p.sportradar_id)
            if sl and sl in sleeper_to_gsis:
                gsis, src = sleeper_to_gsis[sl], "sleeper"
            elif sr and sr in sportradar_to_gsis:
                gsis, src = sportradar_to_gsis[sr], "sportradar"

            if gsis:
                filled += 1
                if src == "sleeper":
                    by_sleeper += 1
                else:
                    by_sportradar += 1
                if dry_run:
                    print(f"  [DRY-RUN] {p.name} ({p.team_abbr}) <- gsis {gsis} (via {src})")
                else:
                    p.gsis_id = gsis
            else:
                still_null.append(f"{p.name} ({p.team_abbr})")

        if not dry_run:
            await session.commit()
    finally:
        if session_ctx is not None:
            await session_ctx.__aexit__(None, None, None)

    logger.info(
        "Kicker gsis backfill: %d candidate null-gsis rows, %d filled "
        "(%d by sleeper, %d by sportradar), %d still null",
        candidates, filled, by_sleeper, by_sportradar, len(still_null),
    )
    if still_null:
        logger.warning(
            "Kicker gsis backfill: %d row(s) not in the nflverse crosswalk "
            "(covered at resolution time by the name+team fallback): %s",
            len(still_null), sorted(still_null)[:12],
        )
    return {
        "candidates": candidates,
        "filled": filled,
        "by_sleeper": by_sleeper,
        "by_sportradar": by_sportradar,
        "still_null": sorted(still_null),
    }


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Backfill gsis_id on kicker rows from nflverse")
    parser.add_argument("--dry-run", action="store_true", help="Show fills without writing")
    args = parser.parse_args()

    result = await backfill_kicker_gsis(dry_run=args.dry_run)
    mode = "DRY-RUN" if args.dry_run else "DONE"
    print(
        f"\n[{mode}] {result['filled']}/{result['candidates']} kicker rows filled "
        f"({result['by_sleeper']} sleeper, {result['by_sportradar']} sportradar); "
        f"{len(result['still_null'])} still null."
    )
    if result["still_null"]:
        print(f"Still null (name+team fallback covers these): {result['still_null'][:12]}")


if __name__ == "__main__":
    asyncio.run(main())
