"""Backfill player identity onto existing league_auction_history rows.

WHY. ``LeagueSyncService._store_picks`` used to write ``player_name=""``,
``position=""`` and no ``player_id``, because Yahoo's ``draftresults`` endpoint returns
only ``player_key``. Every consumer matches on name or player_id, so an entire real
auction sat unusable — the backtest silently fell through to ``market_value_historic``
instead. The writer is fixed; this repairs rows already stored.

Recoverable because ``yahoo_player_key`` was stored: ``461.p.33963`` → the suffix is the
Yahoo player id, which is what ``players.yahoo_id`` holds.

A SCRIPT, NOT A MIGRATION — deliberately. This is a data repair whose correctness depends
on the contents of the ``players`` table, and a migration that mutates rows runs
unattended inside ``alembic upgrade head`` on every deploy. This repo has already been
bitten by putting a data fix in a migration.

AMBIGUOUS KEYS ARE SKIPPED, NEVER GUESSED. Duplicate player rows exist (18 yahoo_ids map
to more than one row); binding a real auction price to the wrong player is worse than
leaving it unresolved.

IDENTITY IS NOT ENOUGH TO MAKE A SEASON SCOREABLE. The backtest keys on
``price > 0 AND player_id IS NOT NULL`` and needs ``MIN_PRICE_COVERAGE`` distinct players
before it will use league prices at all. A row whose ``price`` is 0 stays unusable however
well it resolves — Yahoo omits ``cost`` for snake drafts, so a snake season has no auction
to recover and never will. This script therefore reports, per season, the count that
actually decides it: resolved rows CARRYING A PRICE, against that threshold. "Resolved: N"
on its own has fooled a reader before.

Usage:
    # dev, report only
    .venv/Scripts/python.exe scripts/backfill_auction_identity.py --dry-run

    # dev, apply
    .venv/Scripts/python.exe scripts/backfill_auction_identity.py

    # PROD. Selecting the database is ROOK_ENV_FILE — see backend/config.py. Writes
    # additionally need ROOK_ALLOW_PROD_WRITES=1 (backend/db_guard.py, which keys on the
    # resolved DB HOST). Dry-run prod first; it needs no override.
    ROOK_ENV_FILE=.env.prod .venv/Scripts/python.exe \
        scripts/backfill_auction_identity.py --dry-run
    ROOK_ENV_FILE=.env.prod ROOK_ALLOW_PROD_WRITES=1 .venv/Scripts/python.exe \
        scripts/backfill_auction_identity.py

    (PowerShell: $env:ROOK_ENV_FILE='.env.prod' — and clear it afterwards, it persists.)

    This docstring used to say ``PROD_DATABASE_URL=...``, copied from sibling scripts that
    read that variable themselves. THIS script does not: it connects through
    backend.database.AsyncSessionLocal, i.e. settings.database_url. Setting
    PROD_DATABASE_URL here does nothing, so the "prod" command silently ran against DEV —
    where there is nothing to repair, so it printed "Nothing to backfill" and read as a
    clean bill of health for prod. Every run now prints the host it is touching.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import func, select

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill_auction_identity")


def _quiet_sql() -> None:
    """Silence the SQLAlchemy echo.

    The per-season verdict at the end IS the deliverable of this script, and at root INFO
    the echo buries it under every statement printed twice — on a prod run that verdict is
    the one thing the operator has to read.

    ``engine.echo = False`` is the lever, not ``setLevel``: ``backend.database`` builds the
    engine with ``echo=(environment == "development")``, and an engine created with
    ``echo=True`` wraps its logger in an ``InstanceLogger`` whose ``isEnabledFor`` reports
    INFO regardless of the underlying logger's level. Lowering the level therefore does
    nothing. Called after the import, since that is when the engine exists.
    """
    from backend.database import engine

    engine.echo = False
    for name in ("sqlalchemy.engine", "sqlalchemy.engine.Engine", "sqlalchemy.pool"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def _usable_by_season(db) -> dict[int, int]:
    """Distinct players the backtest can currently price from league_auction_history.

    Mirrors backtest._load_historical_prices' own predicates rather than restating a
    simpler version of them — this number is the point of the exercise.
    """
    from backend.models.league_auction_history import LeagueAuctionHistory as H

    by_id = (await db.execute(
        select(H.season_year, func.count(func.distinct(H.player_id)))
        .where(H.price > 0, H.player_id.isnot(None))
        .group_by(H.season_year)
    )).all()
    by_name = (await db.execute(
        select(H.season_year, func.count(func.distinct(H.player_name)))
        .where(H.price > 0, H.player_id.is_(None),
               H.player_name.isnot(None), H.player_name != "")
        .group_by(H.season_year)
    )).all()
    out: dict[int, int] = {}
    for season, n in by_id:
        out[season] = out.get(season, 0) + int(n or 0)
    for season, n in by_name:
        out[season] = out.get(season, 0) + int(n or 0)
    return out


def build_lookup(player_rows) -> tuple[dict[str, tuple], set[str]]:
    """(yahoo_id -> (player_id, name, position), ambiguous yahoo_ids). Pure.

    A yahoo_id matching more than one player row is dropped from the lookup entirely,
    never resolved to whichever row the query happened to return first.
    """
    lookup: dict[str, tuple] = {}
    ambiguous: set[str] = set()
    for p in player_rows:
        if p.yahoo_id in lookup:
            ambiguous.add(p.yahoo_id)
            continue
        lookup[p.yahoo_id] = (p.id, p.name, p.position)
    for dup in ambiguous:
        lookup.pop(dup, None)
    return lookup, ambiguous


def plan_backfill(rows, lookup, ambiguous, taken):
    """Decide what each id-less row resolves to. PURE — no DB, no mutation.

    `taken` is the set of (player_id, season_year, source) already claimed; it is copied,
    not mutated, so a caller can re-plan. Returns (plan, stats, per_season) where plan is
    a list of (row, player_id, name, position) for the rows that should be written.
    """
    stats = {
        "rows": len(rows), "resolved": 0, "resolved_priced": 0,
        "unmatched": 0, "ambiguous": 0, "no_key": 0, "collision": 0,
        "recovered_spend": 0.0,
    }
    per_season: dict[int, dict] = {}
    claimed = set(taken)
    plan: list[tuple] = []

    for r in rows:
        key = r.yahoo_player_key or ""
        if ".p." not in key:
            stats["no_key"] += 1
            continue
        suffix = key.rsplit(".p.", 1)[-1]
        if suffix in ambiguous:
            stats["ambiguous"] += 1
            continue
        hit = lookup.get(suffix)
        if not hit:
            stats["unmatched"] += 1
            continue
        player_id, name, position = hit

        claim = (player_id, r.season_year, r.source)
        if claim in claimed:
            # Two auction rows claiming one player in one season+source. Skipping is
            # consistent with the ambiguity rule above: an unresolved row costs one
            # price, a wrong one corrupts a scored call. Writing it would raise
            # IntegrityError on uq_auction_player_season_source and roll back the
            # WHOLE repair — these rows only collide at the instant identity is set,
            # because NULL player_id never conflicts in Postgres.
            stats["collision"] += 1
            continue
        claimed.add(claim)

        plan.append((r, player_id, name, position))
        stats["resolved"] += 1
        slot = per_season.setdefault(
            r.season_year, {"resolved": 0, "priced": 0, "spend": 0.0})
        slot["resolved"] += 1
        if (r.price or 0) > 0:
            stats["resolved_priced"] += 1
            stats["recovered_spend"] += float(r.price)
            slot["priced"] += 1
            slot["spend"] += float(r.price)

    return plan, stats, per_season


async def backfill(dry_run: bool = False) -> dict:
    from backend.database import AsyncSessionLocal
    from backend.engines.backtest import MIN_PRICE_COVERAGE
    from backend.models.league_auction_history import LeagueAuctionHistory
    from backend.models.player import Player

    _quiet_sql()

    async with AsyncSessionLocal() as db:
        before_usable = await _usable_by_season(db)

        rows = (await db.execute(
            select(LeagueAuctionHistory).where(
                LeagueAuctionHistory.player_id.is_(None)
            )
        )).scalars().all()
        if not rows:
            logger.info("Nothing to backfill — every row already has a player_id.")
            _report_usable(before_usable, before_usable, MIN_PRICE_COVERAGE)
            return {"rows": 0, "resolved": 0, "resolved_priced": 0, "unmatched": 0,
                    "ambiguous": 0, "no_key": 0, "collision": 0, "recovered_spend": 0.0}

        ids = {
            (r.yahoo_player_key or "").rsplit(".p.", 1)[-1]
            for r in rows
            if ".p." in (r.yahoo_player_key or "")
        }
        player_rows = (await db.execute(
            select(Player.id, Player.yahoo_id, Player.name, Player.position)
            .where(Player.yahoo_id.in_(ids))
        )).all() if ids else []
        lookup, ambiguous = build_lookup(player_rows)

        # (player_id, season, source) already present — uq_auction_player_season_source.
        taken = {
            (pid, season, source)
            for pid, season, source in (await db.execute(
                select(LeagueAuctionHistory.player_id,
                       LeagueAuctionHistory.season_year,
                       LeagueAuctionHistory.source)
                .where(LeagueAuctionHistory.player_id.isnot(None))
            )).all()
        }

        plan, stats, per_season = plan_backfill(rows, lookup, ambiguous, taken)

        if not dry_run:
            for r, player_id, name, position in plan:
                r.player_id = player_id
                if not r.player_name:
                    r.player_name = name or ""
                if not r.position:
                    r.position = position or ""
                db.add(r)
            await db.commit()
        else:
            await db.rollback()

        # Projected, not re-queried, under --dry-run: nothing was written.
        after_usable = dict(before_usable)
        for season, slot in per_season.items():
            after_usable[season] = after_usable.get(season, 0) + slot["priced"]

    logger.info("")
    logger.info("rows missing player_id : %d", stats["rows"])
    logger.info("  resolved             : %d  (of which priced: %d, $%.0f recovered)",
                stats["resolved"], stats["resolved_priced"], stats["recovered_spend"])
    logger.info("  unmatched in players : %d", stats["unmatched"])
    logger.info("  ambiguous (skipped)  : %d", stats["ambiguous"])
    logger.info("  would collide (skip) : %d", stats["collision"])
    logger.info("  no yahoo_player_key  : %d", stats["no_key"])
    if stats["resolved"] and stats["resolved"] != stats["resolved_priced"]:
        logger.info("")
        logger.info(
            "  NOTE: %d resolved row(s) carry price 0 and stay unusable. Yahoo omits "
            "`cost` for SNAKE drafts — a snake season has no auction to recover.",
            stats["resolved"] - stats["resolved_priced"],
        )
    for season in sorted(per_season):
        s = per_season[season]
        logger.info("  season %d: %d resolved, %d priced, $%.0f",
                    season, s["resolved"], s["priced"], s["spend"])

    _report_usable(before_usable, after_usable, MIN_PRICE_COVERAGE)
    logger.info("")
    logger.info("%s", "DRY RUN — nothing written." if dry_run else "Committed.")
    return stats


def _report_usable(before: dict[int, int], after: dict[int, int], threshold: int) -> None:
    """The question the repair actually exists to answer: can the backtest now price
    this season from the league's own auction, or does it still fall through to
    market_value_league (current-season ADP, which makes every signal metric junk)?"""
    seasons = sorted(set(before) | set(after))
    if not seasons:
        logger.info("")
        logger.info("league_auction_history is empty — nothing to score against.")
        return
    logger.info("")
    logger.info("Priceable players per season (backtest needs >= %d):", threshold)
    for season in seasons:
        b, a = before.get(season, 0), after.get(season, 0)
        verdict = "USES LEAGUE PRICES" if a >= threshold else "falls through to ADP"
        arrow = f"{b} -> {a}" if a != b else f"{b}"
        logger.info("  %d: %-14s %s", season, arrow, verdict)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change; write nothing.")
    args = ap.parse_args()

    # ALWAYS say which database this is touching. The old docstring documented a prod
    # invocation this script does not implement, so the "prod repair" ran against dev and
    # reported a clean bill of health for a database it never opened.
    from backend.db_guard import db_host, is_prod_db
    logger.info("backfill_auction_identity: db host=%s  is_prod=%s  mode=%s",
                db_host(), is_prod_db(), "dry-run" if args.dry_run else "APPLY")

    if not args.dry_run:
        # Same host-based guard the pipeline uses: refuses prod unless deliberately
        # overridden. Forgetting keeps you in dev.
        from backend.db_guard import guard_writes
        guard_writes("backfill_auction_identity.py (writes league_auction_history)")

    await backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
