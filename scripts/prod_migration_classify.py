#!/usr/bin/env python
"""READ-ONLY prod safety classification for the dev->prod valuation migration.

Connects to prod with default_transaction_read_only=on (session-level write block).
NEVER sets ROOK_ALLOW_PROD_WRITES. Emits: A/B/C table classification, FK map + cascade
landmines (B/C -> A), protected columns inside class-A, players PK-stability (dev vs prod),
B/C baseline row counts, and the proposed migrate-set.
"""
from __future__ import annotations
import warnings, logging, os, re, asyncio, hashlib
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import asyncpg
from pathlib import Path


def resolve_prod() -> str:
    if os.environ.get("PROD_DATABASE_URL"):
        return os.environ["PROD_DATABASE_URL"]
    for line in Path(".env.prod").read_text().splitlines():
        if line.strip().startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no prod url")


PROD = re.sub(r"\+asyncpg", "", resolve_prod())
DEV = "postgresql://postgres:dev@localhost:5433/rook"
_host = re.sub(r".*@", "", PROD).split("/")[0]
assert any(m in _host for m in ("rlwy.net", "railway")), f"NOT prod: {_host}"

FK_SQL = """
SELECT tc.table_name src, kcu.column_name src_col, ccu.table_name tgt, ccu.column_name tgt_col,
       rc.delete_rule ondel, rc.update_rule onupd
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
  ON tc.constraint_name=ccu.constraint_name AND tc.table_schema=ccu.table_schema
JOIN information_schema.referential_constraints rc
  ON tc.constraint_name=rc.constraint_name AND tc.table_schema=rc.constraint_schema
WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema='public'
ORDER BY src, src_col
"""


async def main() -> None:
    pc = await asyncpg.connect(PROD, server_settings={"default_transaction_read_only": "on"},
                               timeout=25, command_timeout=60)
    print("prod:", re.sub(r":\d+", "", _host), "(read-only session)")
    tabs = [r["tablename"] for r in await pc.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
    fks = await pc.fetch(FK_SQL)
    ucols = {r["table_name"] for r in await pc.fetch(
        "SELECT DISTINCT table_name FROM information_schema.columns "
        "WHERE table_schema='public' AND column_name='user_id'")}

    to_users, to_players = {}, {}
    for f in fks:
        if f["tgt"] == "users":
            to_users.setdefault(f["src"], []).append(f["src_col"])
        if f["tgt"] == "players":
            to_players.setdefault(f["src"], []).append(f["src_col"])

    cls = {}
    for t in tabs:
        u = (t == "users") or (t in to_users) or (t in ucols)
        p = (t == "players") or (t in to_players)
        if t == "players":
            cls[t] = "A"
        elif u and (t in to_players):
            cls[t] = "C"
        elif u:
            cls[t] = "B"
        else:
            cls[t] = "A"

    print(f"\n=== CLASSIFICATION ({len(tabs)} tables) ===")
    for t in tabs:
        drv = ""
        if cls[t] in ("B", "C"):
            bits = []
            if t == "users": bits.append("IS users")
            if t in to_users: bits.append(f"FK {to_users[t]}->users")
            elif t in ucols: bits.append("user_id col (no FK)")
            if t in to_players: bits.append(f"FK {to_players[t]}->players")
            drv = "  [" + "; ".join(bits) + "]"
        print(f"  {cls[t]}  {t:32s}{drv}")

    print("\n=== FULL FK MAP (source.col -> target.col : ON DELETE / ON UPDATE) ===")
    for f in fks:
        tag = ""
        if cls.get(f["src"]) in ("B", "C") and cls.get(f["tgt"]) == "A":
            tag = "   <<< CASCADE LANDMINE (B/C -> A)"
        print(f"  [{cls.get(f['src'])}->{cls.get(f['tgt'])}] {f['src']}.{f['src_col']} -> "
              f"{f['tgt']}.{f['tgt_col']}  ({f['ondel']} / {f['onupd']}){tag}")

    print("\n=== PROTECTED COLUMNS inside class-A (user-coupled cols that must NOT be overwritten) ===")
    prot = 0
    for t in [x for x in tabs if cls[x] == "A"]:
        cols = await pc.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=$1 AND "
            "(column_name='user_id' OR column_name LIKE 'user\\_%' OR column_name LIKE '%\\_user' "
            " OR column_name LIKE '%user_id%')", t)
        names = [c["column_name"] for c in cols]
        if t in to_users:
            names += [f"{c}->users(FK)" for c in to_users[t]]
        if names:
            print(f"  {t}: {names}"); prot += 1
    if not prot:
        print("  (none — no class-A table carries a user-coupled column)")

    print("\n=== BASELINE ROW COUNTS — class B/C (assert byte-unchanged post-migration) ===")
    for t in [x for x in tabs if cls[x] in ("B", "C")]:
        n = await pc.fetchval(f'SELECT count(*) FROM "{t}"')
        print(f"  {cls[t]}  {t:32s} {n}")

    # PK stability: players.id set, prod vs dev
    prod_ids = [r["id"] for r in await pc.fetch("SELECT id FROM players")]
    await pc.close()
    dc = await asyncpg.connect(DEV, timeout=15)
    dev_ids = [r["id"] for r in await dc.fetch("SELECT id FROM players")]
    await dc.close()
    ps, ds = set(map(str, prod_ids)), set(map(str, dev_ids))
    h = lambda s: hashlib.sha256(",".join(sorted(s)).encode()).hexdigest()[:12]
    print("\n=== PLAYER PK STABILITY (players.id: prod vs dev) ===")
    print(f"  prod players={len(ps)}  dev players={len(ds)}")
    print(f"  in prod not dev: {len(ps - ds)}   in dev not prod: {len(ds - ps)}")
    print(f"  id-set sha (prod)={h(ps)}  (dev)={h(ds)}  MATCH={h(ps)==h(ds)}")


if __name__ == "__main__":
    asyncio.run(main())
