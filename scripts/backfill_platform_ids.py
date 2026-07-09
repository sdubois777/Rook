"""
Backfill players.espn_id + players.yahoo_id for DETERMINISTIC platform roster
resolution.

Sources (union): nfl.import_ids() PRIMARY (joins to a Rook player via
sleeper_id → sportradar_id → gsis_id; its rows carry both espn_id and yahoo_id),
the Sleeper /players/nfl dump as FILL (lifts union coverage — measured ~80% of
relevant players, 100% of startable offense). Both ids are normalised to a bare
string (Sleeper's yahoo_id is already the bare numeric tail of Yahoo's
"449.p.<id>" player_key).

IDEMPOTENT: fills a NULL column only, never overwrites. Re-runnable. Reports
coverage counts loudly. Does NOT touch the yahoo_player_id TRAP column
("nfl_"+gsis_id).

Usage:  uv run python scripts/backfill_platform_ids.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill_platform_ids")


def _norm(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _build_import_ids_maps() -> dict[str, dict[str, tuple[str | None, str | None]]]:
    """{key_type: {rook-side id -> (espn_id, yahoo_id)}} from nfl.import_ids()."""
    import nfl_data_py as nfl

    ids = nfl.import_ids()
    maps = {"sleeper": {}, "sportradar": {}, "gsis": {}}
    col = {"sleeper": "sleeper_id", "sportradar": "sportradar_id", "gsis": "gsis_id"}
    for row in ids.itertuples(index=False):
        e = _norm(getattr(row, "espn_id", None))
        y = _norm(getattr(row, "yahoo_id", None))
        if not (e or y):
            continue
        for kind, c in col.items():
            k = _norm(getattr(row, c, None))
            if k:
                maps[kind].setdefault(k, (e, y))
    return maps


def _build_sleeper_maps() -> dict[str, dict[str, tuple[str | None, str | None]]]:
    """Same shape from the Sleeper /players/nfl dump (FILL source)."""
    import httpx

    maps = {"sleeper": {}, "sportradar": {}, "gsis": {}}
    try:
        dump = httpx.get("https://api.sleeper.app/v1/players/nfl", timeout=60).json()
    except Exception as exc:
        logger.warning("Sleeper dump fetch failed (%s) — import_ids only", exc)
        return maps
    for pid, p in dump.items():
        e = _norm(p.get("espn_id"))
        y = _norm(p.get("yahoo_id"))
        if not (e or y):
            continue
        if (k := _norm(pid)):
            maps["sleeper"].setdefault(k, (e, y))
        if (k := _norm(p.get("sportradar_id"))):
            maps["sportradar"].setdefault(k, (e, y))
        if (k := _norm(p.get("gsis_id"))):
            maps["gsis"].setdefault(k, (e, y))
    return maps


def _lookup(maps, sleeper, sportradar, gsis) -> tuple[str | None, str | None]:
    """Resolve (espn, yahoo) by rook-side id priority: sleeper → sportradar → gsis."""
    for kind, key in (("sleeper", sleeper), ("sportradar", sportradar), ("gsis", gsis)):
        if key and key in maps[kind]:
            return maps[kind][key]
    return None, None


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill players.espn_id / yahoo_id")
    parser.add_argument("--dry-run", action="store_true", help="report only, no writes")
    args = parser.parse_args()

    from sqlalchemy import select
    from backend.database import AsyncSessionLocal
    from backend.models.player import Player

    logger.info("Building crosswalks (import_ids primary + Sleeper fill)…")
    ii = _build_import_ids_maps()
    sl = _build_sleeper_maps()
    logger.info(
        "  import_ids keys: sleeper=%d sportradar=%d gsis=%d | sleeper-dump keys: sleeper=%d sportradar=%d gsis=%d",
        len(ii["sleeper"]), len(ii["sportradar"]), len(ii["gsis"]),
        len(sl["sleeper"]), len(sl["sportradar"]), len(sl["gsis"]),
    )

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Player.id, Player.sleeper_id, Player.sportradar_id, Player.gsis_id,
                   Player.espn_id, Player.yahoo_id, Player.position)
        )).all()

        total = len(rows)
        filled_espn = filled_yahoo = from_ii = from_sl = 0
        for pid, sleeper, sportradar, gsis, cur_espn, cur_yahoo, pos in rows:
            if cur_espn and cur_yahoo:
                continue  # already populated
            skey, srkey, gkey = _norm(sleeper), _norm(sportradar), _norm(gsis)
            # Fill EACH id independently from the UNION: import_ids is primary, but a
            # row with espn and NO yahoo must still take the Sleeper-dump yahoo fill
            # (and vice-versa) — never take one source's whole tuple wholesale.
            e_ii, y_ii = _lookup(ii, skey, srkey, gkey)
            e_sl, y_sl = _lookup(sl, skey, srkey, gkey)
            e = e_ii or e_sl
            y = y_ii or y_sl
            from_ii += bool(e_ii or y_ii)
            from_sl += bool((e_sl and not e_ii) or (y_sl and not y_ii))
            if not (e or y):
                continue
            new_espn = cur_espn or e            # fill NULL only — never overwrite
            new_yahoo = cur_yahoo or y
            if new_espn == cur_espn and new_yahoo == cur_yahoo:
                continue
            if not args.dry_run:
                p = await db.get(Player, pid)
                if new_espn and not p.espn_id:
                    p.espn_id = new_espn
                if new_yahoo and not p.yahoo_id:
                    p.yahoo_id = new_yahoo
            if new_espn and not cur_espn:
                filled_espn += 1
            if new_yahoo and not cur_yahoo:
                filled_yahoo += 1

        if not args.dry_run:
            await db.commit()

        # Final coverage snapshot (post-commit)
        after = (await db.execute(
            select(Player.espn_id, Player.yahoo_id)
        )).all()
        cov_e = sum(1 for e, _y in after if e)
        cov_y = sum(1 for _e, y in after if y)

    logger.info(
        "%s: filled espn_id=%d yahoo_id=%d (import_ids=%d players, sleeper-fill=%d)",
        "DRY-RUN" if args.dry_run else "DONE", filled_espn, filled_yahoo, from_ii, from_sl,
    )
    logger.info(
        "coverage now: espn_id %d/%d (%d%%) · yahoo_id %d/%d (%d%%)",
        cov_e, total, 100 * cov_e // max(total, 1),
        cov_y, total, 100 * cov_y // max(total, 1),
    )


if __name__ == "__main__":
    asyncio.run(main())
