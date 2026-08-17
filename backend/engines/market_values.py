"""
Market value sync engine — scrapes FantasyPros auction values and updates
market_value fields on Player records.

Called by:
  - scripts/refresh_market_values.py (CLI)
  - POST /pipeline/refresh-market-values (API)
  - POST /admin/pipeline/run with agent_name="market_values" (Admin UI)
"""
from __future__ import annotations

import asyncio
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.integrations.nfl_data import _normalize_team_abbr, normalize_player_name
from backend.models.market_value_historic import (
    MarketValueHistoric, SOURCE_FANTASYPROS_CONSENSUS,
)
from backend.models.player import Player
from backend.utils.seasons import asof_active, get_current_season, season_for_date

logger = logging.getLogger(__name__)

# A scrape smaller than this is treated as broken rather than as news. The DraftWizard
# page is parsed after a fixed wait and rows with too few cells are dropped
# (backend/integrations/fantasypros.py), so a half-rendered table yields a SHORT list,
# not an empty one — and the only previous guard was `if not values`, strict emptiness.
# A five-row scrape therefore updated five players, committed, recorded a healthy
# refresh, and left every other price untouched at whatever age it already was.
# 100 matches the "Minimum viable result: 100+ players" already documented for
# get_best_available_auction_year in backend/utils/seasons.py.
MIN_SCRAPE_ROWS = 100

# ...and a scrape must also not collapse against the PREVIOUS SUCCESSFUL SCRAPE, which
# the absolute floor alone cannot catch: a drop from 350 rows to 120 is equally a broken
# page.
#
# The comparison is against the last recorded scrape size (market_value_metadata), NOT
# against how many players currently hold a price. Those are different numbers and only
# one of them is stable. Nothing ever clears a price, so the count of priced rows is the
# union of every scrape ever run and only grows; a floor derived from it would climb past
# the real scrape size and then refuse every healthy run, turning this guard into a
# permanent outage.
MIN_SCRAPE_FRACTION_OF_PREVIOUS = 0.8

# Playwright needs ProactorEventLoop on Windows for subprocess support.
# Uvicorn uses SelectorEventLoop, so we run the scrape in a dedicated thread
# with its own event loop.
_pw_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")


def _scrape_in_thread(scoring_format: str, teams: int) -> tuple[list[dict], int, bool]:
    """Run the Playwright scrape in a thread with a fresh event loop."""
    from backend.integrations.fantasypros import get_best_auction_values

    loop = asyncio.new_event_loop()
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    try:
        return loop.run_until_complete(
            get_best_auction_values(format=scoring_format, teams=teams)
        )
    finally:
        loop.close()


async def _previous_scrape_size(session: AsyncSession) -> int | None:
    """How many players the last recorded refresh matched, or None if there is none.

    Read from market_value_metadata, which records one row per successful refresh. This
    is a STABLE basis for "did the page collapse": it reflects one scrape, and it goes
    down as well as up. The count of players currently holding a price does not — no
    code ever clears a price, so that count is the union of every scrape ever run.
    """
    try:
        from backend.models.market_value_metadata import MarketValueMetadata
        return (await session.execute(
            select(MarketValueMetadata.player_count)
            .order_by(MarketValueMetadata.refreshed_at.desc())
            .limit(1)
        )).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — table may not exist yet; fall back to the floor
        logger.debug("No market value metadata available for the scrape-size check")
        return None


def _snapshot_payload(rows) -> tuple[list[dict], int]:
    """(rows to archive, count skipped for having no timestamp).

    Pure, so the season-labelling rule is testable without a database. Each price is
    labelled with the season ITS OWN timestamp falls in — see the caller's docstring
    for why reading the clock instead is an off-by-one at every season boundary.
    """
    payload: list[dict] = []
    skipped = 0
    for row in rows:
        if row.market_value_updated_at is None:
            skipped += 1
            continue
        payload.append({
            "player_id": row.id,
            "season_year": season_for_date(row.market_value_updated_at),
            "price": float(row.market_value_fantasypros),
            "source": SOURCE_FANTASYPROS_CONSENSUS,
        })
    return payload, skipped


async def _snapshot_current_market_values(session: AsyncSession) -> int:
    """
    Archive the price currently in market_value_fantasypros before the scrape
    overwrites it, into market_value_historic as a FantasyPros consensus estimate.

    EACH ROW IS LABELLED WITH THE SEASON ITS OWN PRICE CAME FROM, read from that
    player's market_value_updated_at — not from today's clock. This runs BEFORE the
    scrape precisely because the column still holds the PREVIOUS run's price, so
    stamping get_current_season() mislabelled every carried-over price at every season
    boundary: a price scraped in one season and archived by the first run after the
    following March was recorded under the later year.

    A row whose market_value_updated_at is NULL is SKIPPED. That is the marker for a
    price this sync did not put there — specifically the residue left by the as-of
    market seeder (_seed_asof_market in scripts/run_predraft_pipeline.py), which fills
    this column with a PAST season's realized auction prices. Archiving those as the
    present season's consensus would file real prices from one year under another.

    Writes are ON CONFLICT DO UPDATE, so re-running converges on the latest observation
    for the season it belongs to. That is safe only because ``source`` is part of the
    unique key: realized league auction prices live under a different source and cannot
    be touched by anything here.
    """
    result = await session.execute(
        select(
            Player.id,
            Player.market_value_fantasypros,
            Player.market_value_updated_at,
        )
        .where(
            Player.market_value_fantasypros.isnot(None),
            Player.position.in_(["QB", "RB", "WR", "TE"]),
        )
    )
    rows = result.all()
    if not rows:
        return 0

    payload, skipped_no_provenance = _snapshot_payload(rows)

    if skipped_no_provenance:
        logger.warning(
            "Snapshot skipped %d player(s) whose price has no timestamp — the season "
            "that price belongs to cannot be established, so it is not archived.",
            skipped_no_provenance,
        )

    if not payload:
        return 0

    stmt = pg_insert(MarketValueHistoric).values(payload)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_market_value_historic",
        set_={"price": stmt.excluded.price},
    )
    await session.execute(stmt)
    await session.flush()

    seasons = sorted({p["season_year"] for p in payload})
    logger.info(
        "Snapshotted %d consensus market value(s) into historic for season(s) %s",
        len(payload), seasons,
    )
    return len(payload)


_DST_ALIASES = {"DST", "DEF", "D/ST"}


def _canonical_position(raw: str | None) -> str:
    p = (raw or "").upper()
    return "DST" if p in _DST_ALIASES else p


def _resolve_scraped_player(
    row: dict, player_index: dict[str, list[Player]],
) -> tuple[Player | None, str]:
    """Pick the one player a scraped auction row refers to.

    Returns (player, reason). reason is "ok", "unmatched" (no row of that name) or
    "ambiguous" (several rows and no way to choose). An ambiguous row gets NO price:
    a missing price is visible and self-correcting, a price on the wrong player is
    neither.

    The DraftWizard feed carries only name, team and position, so the id-first chain
    CLAUDE.md rule 7 requires is not available here. Position is, and it is the check
    that separates a genuine duplicate from two different people who share a name —
    the same approach already used for this identical feed by
    build_format_market_upserts in backend/services/format_market_ingest.py.
    """
    candidates = player_index.get(normalize_player_name(row.get("name")), [])
    if not candidates:
        return None, "unmatched"
    if len(candidates) == 1:
        return candidates[0], "ok"

    scraped_pos = _canonical_position(row.get("position"))
    if scraped_pos:
        narrowed = [c for c in candidates if _canonical_position(c.position) == scraped_pos]
        if not narrowed:
            # Several rows answer to this name and NONE plays the position the feed says.
            # Falling through to the tiebreak here would hand the price to a player at
            # the wrong position, which is the failure this function exists to stop.
            return None, "ambiguous"
        candidates = narrowed
    if len(candidates) == 1:
        return candidates[0], "ok"

    # Team next. Unlike position this is a HINT, not a constraint: a player who changed
    # teams recently can carry a stale team in either source, so a team that matches
    # nothing narrows nothing rather than rejecting the row.
    #
    # This is what separates the two Frank Gore rows this database holds — both stored
    # as "Frank Gore", both running backs, one aged 24 on Buffalo and one aged 38 with
    # no team. They are different people, the name normalizer strips the generational
    # suffix so the feed's "Frank Gore Jr." collides with both, and CLAUDE.md names this
    # exact pair as a case that must never be resolved on a name key.
    scraped_team = _normalize_team_abbr(row.get("team") or "")
    if scraped_team:
        by_team = [
            c for c in candidates
            if c.team_abbr and _normalize_team_abbr(c.team_abbr) == scraped_team
        ]
        if len(by_team) == 1:
            return by_team[0], "ok"
        if by_team:
            candidates = by_team

    # Still several. Prefer the row the board actually renders — a valued row carries a
    # bid ceiling, a leftover duplicate shell row does not. This is what stopped
    # Kenneth Walker's price landing on a team-less receiver row while the real running
    # back on Kansas City kept a price six days older.
    valued = [c for c in candidates if c.recommended_bid_ceiling is not None]
    if len(valued) == 1:
        return valued[0], "ok"

    return None, "ambiguous"


async def sync_market_values(
    session: AsyncSession,
    scoring_format: str = "ppr",
    teams: int = 12,
    dry_run: bool = False,
) -> dict:
    """
    Scrape FantasyPros auction values and update market_value fields.

    Automatically determines which year to use via
    get_best_available_auction_year() — current season if July+,
    fallback to previous season otherwise.

    Args:
        session: Active async DB session.
        scoring_format: "ppr" | "half_ppr" | "standard". League is full PPR.
        teams: Number of teams in league.
        dry_run: If True, show matches without writing to DB.

    Returns:
        Summary dict with matched/unmatched counts, year info, and names.
    """
    # REFUSE under an as-of clock. This is a live current-season scrape, so on a
    # past-dated board it would overwrite that season's real prices with next year's
    # consensus, and every buy/sell signal on that board is computed against the market.
    # The pipeline already skips this stage, but the CLI (scripts/refresh_market_values.py)
    # and the API endpoint (backend/routers/pipeline.py) call straight in here, so the
    # refusal belongs at the engine where all three callers inherit it.
    if asof_active():
        note = (
            "Refused: an as-of clock (ROOK_ASOF_DATE) is set. A live scrape would "
            "overwrite that season's real prices with current-season consensus. Unset "
            "ROOK_ASOF_DATE to refresh the live market."
        )
        logger.warning("sync_market_values %s", note)
        return {
            "matched": 0, "unmatched": 0, "unmatched_names": [], "updated_at": None,
            "year": None, "is_current_season": None, "error": note,
        }

    # How large the last successful scrape was — the basis for the collapse check below.
    # None on a first-ever run, which then relies on the absolute floor alone.
    previous_scrape_size = await _previous_scrape_size(session)

    # Snapshot current values before overwriting
    if not dry_run:
        await _snapshot_current_market_values(session)

    logger.info("Scraping FantasyPros market values (format=%s, teams=%d)...", scoring_format, teams)
    try:
        loop = asyncio.get_running_loop()
        values, year_used, is_current = await loop.run_in_executor(
            _pw_executor, _scrape_in_thread, scoring_format, teams
        )
    except Exception as exc:
        logger.error("FantasyPros scrape failed: %s", exc)
        # Discard the flushed snapshot explicitly. Abandoning the session would also
        # drop it, but only by accident of the caller closing without committing.
        await session.rollback()
        return {
            "matched": 0,
            "unmatched": 0,
            "unmatched_names": [],
            "updated_at": None,
            "year": None,
            "is_current_season": None,
            "error": str(exc),
        }

    if not values:
        logger.warning(
            "FantasyPros returned no data — auction values may not be published yet."
        )
        await session.rollback()
        return {
            "matched": 0,
            "unmatched": 0,
            "unmatched_names": [],
            "updated_at": None,
            "year": None,
            "is_current_season": None,
            "note": "No data available from FantasyPros (not yet published for this season)",
        }

    # A SHORT scrape is a broken page, not news. Writing it would update a handful of
    # players, commit, and report a healthy refresh while leaving every other price at
    # whatever age it already had — with nothing on any surface saying so.
    relative_floor = (
        int(previous_scrape_size * MIN_SCRAPE_FRACTION_OF_PREVIOUS)
        if previous_scrape_size else 0
    )
    floor = max(MIN_SCRAPE_ROWS, relative_floor)
    if len(values) < floor:
        basis = (
            f"{MIN_SCRAPE_ROWS} minimum, or "
            f"{int(MIN_SCRAPE_FRACTION_OF_PREVIOUS * 100)}% of the "
            f"{previous_scrape_size} matched by the previous refresh"
            if previous_scrape_size else f"{MIN_SCRAPE_ROWS} minimum"
        )
        note = (
            f"Refused: scrape returned {len(values)} row(s), below the floor of {floor} "
            f"({basis}). Prices left unchanged — a partial scrape would "
            f"silently leave most of the board stale while reporting success."
        )
        logger.error("sync_market_values %s", note)
        await session.rollback()
        return {
            "matched": 0, "unmatched": 0, "unmatched_names": [], "updated_at": None,
            "year": year_used, "is_current_season": is_current, "error": note,
        }

    # --- Load all players from DB ---
    players: list[Player] = (
        await session.execute(select(Player))
    ).scalars().all()

    # Normalized name → EVERY player with that name. A plain dict keyed on the name lost
    # one row per collision by assignment, and never looked at position — so a scraped
    # price could land on a same-named player at another position while the row the
    # board actually renders got nothing. Measured on this database: Antonio Williams'
    # price went to a team-less RB row while the real WR on Washington kept no price at
    # all, which drops him from the board entirely (draftable_filter in
    # backend/repositories/player_repo.py gates on this column).
    player_index: dict[str, list[Player]] = {}
    for p in players:
        key = normalize_player_name(p.name)
        if key:
            player_index.setdefault(key, []).append(p)

    # --- Match scraped data to DB players ---
    now = datetime.now(timezone.utc)
    matched = 0
    unmatched_names: list[str] = []
    ambiguous_names: list[str] = []

    for row in values:
        name = row.get("name", "")
        avg_value = row.get("avg_value")
        if avg_value is None:
            continue

        player, reason = _resolve_scraped_player(row, player_index)
        if player is None:
            if reason == "ambiguous":
                ambiguous_names.append(name)
            else:
                unmatched_names.append(name)
            continue

        matched += 1

        if dry_run:
            logger.info(
                "DRY RUN: %s → %s (%s) auction=$%.1f",
                name, player.name, player.position, avg_value,
            )
            continue

        # Write market value fields. market_value and market_value_fantasypros are
        # deliberately the same number: this is the only writer of either, and the
        # Teams page and live-draft engine read market_value while the draft board
        # reads market_value_fantasypros.
        player.market_value = avg_value
        player.market_value_fantasypros = avg_value
        player.market_value_confidence = _compute_confidence(row)
        player.market_value_updated_at = now
        session.add(player)

    # --- Write metadata ---
    if not dry_run and matched > 0:
        await _store_metadata(session, {
            "source": "fantasypros",
            "year": year_used,
            "is_current_season": is_current,
            "player_count": matched,
            "refreshed_at": now,
        })

    if not dry_run:
        await session.commit()

    if unmatched_names:
        logger.warning(
            "%d unmatched FantasyPros names: %s",
            len(unmatched_names),
            unmatched_names[:20],
        )
    if ambiguous_names:
        # Reported separately from unmatched: these DID match a name, and were skipped
        # on purpose because more than one player row answered to it. Folding them into
        # "unmatched" would hide the duplicate-row problem that caused them.
        logger.warning(
            "%d FantasyPros name(s) matched several player rows and were SKIPPED "
            "rather than priced on a guess: %s",
            len(ambiguous_names), ambiguous_names[:20],
        )

    summary = {
        "matched": matched,
        "unmatched": len(unmatched_names),
        "unmatched_names": unmatched_names[:50],
        "ambiguous": len(ambiguous_names),
        "ambiguous_names": ambiguous_names[:50],
        "updated_at": now.isoformat() if not dry_run else None,
        "year": year_used,
        "is_current_season": is_current,
        "dry_run": dry_run,
    }
    logger.info(
        "Market value sync %s: %d matched, %d unmatched, %d ambiguous "
        "(year=%d, current=%s)",
        "DRY RUN" if dry_run else "complete",
        matched, len(unmatched_names), len(ambiguous_names), year_used, is_current,
    )
    return summary


def _compute_confidence(data: dict) -> str:
    """
    Derive confidence from the spread between min and max auction values.
    If min/max not available (DraftWizard), default to 'medium'.
    """
    avg = data.get("avg_value")
    min_val = data.get("min_value")
    max_val = data.get("max_value")

    if avg is None or min_val is None or max_val is None:
        return "medium"

    if avg <= 0:
        return "low"

    spread = max_val - min_val
    spread_pct = spread / avg if avg > 0 else 1.0

    if spread_pct <= 0.3:
        return "high"
    if spread_pct <= 0.6:
        return "medium"
    return "low"


async def seed_prior_season_from_auction_history(
    session: AsyncSession,
    season_year: int | None = None,
) -> dict:
    """
    Populate market_value_prior_season from league_auction_history.

    Uses the most recent completed season's auction data (AVG price per player).
    """
    from sqlalchemy import text as sa_text
    from backend.utils.seasons import get_previous_season

    target_year = season_year or get_previous_season()

    # Get average price per player_name from auction history for the target season
    result = await session.execute(sa_text(
        'SELECT player_name, AVG(price) AS avg_price '
        'FROM league_auction_history '
        'WHERE season_year = :yr AND price > 0 '
        'GROUP BY player_name'
    ), {'yr': target_year})
    rows = result.all()

    if not rows:
        return {"updated": 0, "season_year": target_year, "note": "No auction history data"}

    # Build normalized lookup
    price_lookup: dict[str, float] = {}
    for name, avg_price in rows:
        key = normalize_player_name(name)
        if key:
            price_lookup[key] = float(avg_price)

    # Load all players
    players: list[Player] = (
        await session.execute(select(Player))
    ).scalars().all()

    updated = 0
    for p in players:
        key = normalize_player_name(p.name)
        if key and key in price_lookup:
            p.market_value_prior_season = price_lookup[key]
            p.market_value_prior_season_year = target_year
            session.add(p)
            updated += 1

    await session.commit()
    logger.info("Seeded prior season prices: %d players from %d auction history", updated, target_year)
    return {"updated": updated, "season_year": target_year}


async def _store_metadata(session: AsyncSession, data: dict) -> None:
    """Write a MarketValueMetadata record for sourcing info."""
    try:
        from backend.models.market_value_metadata import MarketValueMetadata
        record = MarketValueMetadata(
            source=data["source"],
            year=data["year"],
            is_current_season=data["is_current_season"],
            player_count=data["player_count"],
            refreshed_at=data["refreshed_at"],
        )
        session.add(record)
    except Exception:
        # Table may not exist yet (migration not run) — don't block sync
        logger.debug("MarketValueMetadata table not available, skipping metadata write")
