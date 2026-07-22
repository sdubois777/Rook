#!/usr/bin/env python
"""Migrate the DEV board -> PROD so prod == dev for player/board data, leaving all
user/account + runtime/audit tables and the 3 user-derived players columns untouched.

Guarded: refuses unless prod host + ROOK_ALLOW_PROD_WRITES=1. Atomic: one prod
transaction; in-transaction count sanity or rollback. FK-ordered. JSON pass-through.

players : UPDATE by id, ALL columns except id + the 3 preserved user-derived columns.
tables  : wholesale replace (DELETE prod + INSERT dev), FK-ordered.
"""
from __future__ import annotations
import warnings, logging, os, re, asyncio
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import asyncpg
from pathlib import Path
from urllib.parse import urlparse

from backend.db_guard import is_prod_db, prod_override_active


def prod_url() -> str:
    if os.environ.get("PROD_DATABASE_URL"):
        return os.environ["PROD_DATABASE_URL"]
    for l in Path(".env.prod").read_text().splitlines():
        if l.strip().startswith("DATABASE_URL="):
            return l.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no prod url")


PROD = re.sub(r"\+asyncpg", "", prod_url())
DEV = "postgresql://postgres:dev@localhost:5433/rook"

PRESERVED = {"market_value_league", "market_value_prior_season", "market_value_prior_season_year"}

# Board tables — wholesale. opponent_profiles -> draft_state is the only inter-board FK.
BOARD = ["player_profiles", "player_format_values", "player_dependencies",
         "player_injury_profiles", "player_schedules", "team_systems",
         "beat_reporter_signals", "market_value_historic", "market_value_metadata",
         "season_roster", "value_snapshots", "draft_state", "opponent_profiles"]
DELETE_FIRST = ["opponent_profiles"]            # child before parent
INSERT_LAST = ["opponent_profiles"]             # parent (draft_state) before child


async def _codec(conn):
    for t in ("json", "jsonb"):
        await conn.set_type_codec(t, encoder=lambda v: v, decoder=lambda v: v,
                                  schema="pg_catalog", format="text")


async def main() -> None:
    if not is_prod_db(PROD):
        raise SystemExit("target is not prod")
    if not prod_override_active():
        raise SystemExit("ROOK_ALLOW_PROD_WRITES not set — refusing prod write")

    dev = await asyncpg.connect(DEV, timeout=20); await _codec(dev)
    prod = await asyncpg.connect(PROD, timeout=30, command_timeout=180); await _codec(prod)
    print("target prod:", re.sub(r"\..*", "", urlparse(PROD).hostname), "| override ACTIVE")

    # ---- read all dev data first ----
    pcols = [r["column_name"] for r in await prod.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
        "AND table_name='players' ORDER BY ordinal_position")]
    set_cols = [c for c in pcols if c != "id" and c not in PRESERVED]
    dev_players = await dev.fetch("SELECT " + ",".join(f'"{c}"' for c in (["id"] + set_cols)) + " FROM players")
    dev_board = {t: await dev.fetch(f'SELECT * FROM "{t}"') for t in BOARD}
    dev_counts = {t: len(dev_board[t]) for t in BOARD}
    print(f"read dev: players={len(dev_players)} (updating {len(set_cols)} cols; preserving {sorted(PRESERVED)})")
    print("dev board counts:", dev_counts)

    async with prod.transaction():
        # 1) players UPDATE by id
        assign = ", ".join(f'"{c}"=${i+2}' for i, c in enumerate(set_cols))
        await prod.executemany(
            f"UPDATE players SET {assign} WHERE id=$1",
            [[r["id"], *[r[c] for c in set_cols]] for r in dev_players])
        print(f"players: UPDATEd {len(dev_players)} rows")

        # 2) DELETE board (children first)
        for t in DELETE_FIRST + [x for x in BOARD if x not in DELETE_FIRST]:
            await prod.execute(f'DELETE FROM "{t}"')
        # 3) INSERT board (parents first, opponent_profiles last)
        for t in [x for x in BOARD if x not in INSERT_LAST] + INSERT_LAST:
            rows = dev_board[t]
            if not rows:
                continue
            cols = list(rows[0].keys())
            ph = ", ".join(f"${i+1}" for i in range(len(cols)))
            await prod.executemany(
                f'INSERT INTO "{t}" ({",".join(chr(34)+c+chr(34) for c in cols)}) VALUES ({ph})',
                [[r[c] for c in cols] for r in rows])
            print(f"  {t}: inserted {len(rows)}")

        # 4) in-transaction sanity — counts must match dev, else raise -> rollback
        pn = await prod.fetchval("SELECT count(*) FROM players")
        if pn != len(dev_players):
            raise RuntimeError(f"players count {pn} != dev {len(dev_players)}")
        for t in BOARD:
            n = await prod.fetchval(f'SELECT count(*) FROM "{t}"')
            if n != dev_counts[t]:
                raise RuntimeError(f"{t} count {n} != dev {dev_counts[t]}")
        print("in-transaction sanity OK — committing")

    print("COMMITTED")
    await dev.close(); await prod.close()


if __name__ == "__main__":
    asyncio.run(main())
