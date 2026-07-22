#!/usr/bin/env python
"""Post-migration validation.
(a) PRESERVE tables: prod-live == pre-write snapshot (byte-identical), users=10.
(b) preserved players cols (3): prod-live == snapshot.
(c) migrated: prod-live == dev (players 71 cols + 13 board tables).
Read-only everywhere.
"""
from __future__ import annotations
import warnings, logging, os, re, asyncio, hashlib
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import asyncpg
from pathlib import Path


def prod_url() -> str:
    if os.environ.get("PROD_DATABASE_URL"):
        return os.environ["PROD_DATABASE_URL"]
    for l in Path(".env.prod").read_text().splitlines():
        if l.strip().startswith("DATABASE_URL="):
            return l.split("=", 1)[1].strip().strip('"').strip("'")


PROD = re.sub(r"\+asyncpg", "", prod_url())
SNAP = "postgresql://postgres:dev@localhost:5433/prodsnap"
DEV = "postgresql://postgres:dev@localhost:5433/rook"

PRESERVE = ["users", "user_preferences", "user_leagues", "platform_credentials",
            "draft_sessions", "credit_usage_log", "granted_monthly_invoices",
            "granted_pack_sessions", "league_auction_history", "processed_stripe_events",
            "agent_cache", "api_usage_log", "player_cleanup_audit", "player_merge_audit",
            "alembic_version"]
BOARD = ["player_profiles", "player_format_values", "player_dependencies",
         "player_injury_profiles", "player_schedules", "team_systems",
         "beat_reporter_signals", "market_value_historic", "market_value_metadata",
         "season_roster", "value_snapshots", "draft_state", "opponent_profiles"]
PRESERVED_COLS = ["market_value_league", "market_value_prior_season", "market_value_prior_season_year"]


async def _c(dsn, ro=False):
    conn = await asyncpg.connect(dsn, timeout=30, command_timeout=120,
                                 server_settings={"default_transaction_read_only": "on"} if ro else None)
    for t in ("json", "jsonb"):
        await conn.set_type_codec(t, encoder=lambda v: v, decoder=lambda v: v,
                                  schema="pg_catalog", format="text")
    return conn


async def thash(conn, table, cols):
    rows = await conn.fetch(f'SELECT {",".join(chr(34)+c+chr(34) for c in cols)} FROM "{table}"')
    hs = sorted(hashlib.md5(repr([r[c] for c in cols]).encode()).hexdigest() for r in rows)
    return hashlib.md5("".join(hs).encode()).hexdigest(), len(rows)


async def cols_of(conn, table):
    return [r["column_name"] for r in await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
        "AND table_name=$1 ORDER BY ordinal_position", table)]


async def main():
    prod = await _c(PROD, ro=True); snap = await _c(SNAP); dev = await _c(DEV)
    fails = []

    print("=== (a) PRESERVE tables: prod-live == pre-write snapshot ===")
    for t in PRESERVE:
        cols = await cols_of(prod, t)
        ph, pn = await thash(prod, t, cols); sh, sn = await thash(snap, t, cols)
        ok = (ph == sh)
        if not ok: fails.append(f"PRESERVE {t}")
        print(f"  {'OK ' if ok else 'FAIL'} {t:28s} prod_n={pn} snap_n={sn} {'identical' if ok else 'CHANGED'}")
    users = await prod.fetchval("SELECT count(*) FROM users")
    print(f"  users count (prod-live) = {users}  {'OK' if users==10 else 'FAIL'}")
    if users != 10: fails.append("users!=10")

    print("=== (b) preserved players columns (3): prod-live == snapshot ===")
    ph, _ = await thash(prod, "players", ["id"] + PRESERVED_COLS)
    sh, _ = await thash(snap, "players", ["id"] + PRESERVED_COLS)
    ok = (ph == sh);
    if not ok: fails.append("preserved players cols")
    print(f"  {'OK ' if ok else 'FAIL'} market_value_league/prior_season(_year) {'unchanged' if ok else 'CHANGED'}")

    print("=== (c) MIGRATED: prod-live == dev ===")
    pcols = await cols_of(prod, "players")
    migc = ["id"] + [c for c in pcols if c != "id" and c not in PRESERVED_COLS]
    ph, pn = await thash(prod, "players", migc); dh, dn = await thash(dev, "players", migc)
    ok = (ph == dh)
    if not ok: fails.append("players migrated cols != dev")
    print(f"  {'OK ' if ok else 'FAIL'} players ({len(migc)-1} migrated cols)  prod_n={pn} dev_n={dn} {'== dev' if ok else 'DIFFERS'}")
    for t in BOARD:
        cols = await cols_of(prod, t)
        ph, pn = await thash(prod, t, cols); dh, dn = await thash(dev, t, cols)
        ok = (ph == dh)
        if not ok: fails.append(f"board {t} != dev")
        print(f"  {'OK ' if ok else 'FAIL'} {t:28s} prod_n={pn} dev_n={dn} {'== dev' if ok else 'DIFFERS'}")

    print("\n=== RESULT:", "ALL CLEAN ✓" if not fails else f"FAILURES: {fails}", "===")
    await prod.close(); await snap.close(); await dev.close()


if __name__ == "__main__":
    asyncio.run(main())
