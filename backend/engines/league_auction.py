"""
League Auction Engine — import and manage historical league auction prices.

Entry points:
  1. import_league_auction_csv()    — Parse CSV from Yahoo Draft Recap copy-paste
  2. sync_league_auction_from_yahoo() — Pull current season from Yahoo API
  3. sync_all_league_history()       — Auto-discover and pull ALL historical seasons
  4. refresh_market_value_league()   — Set player.market_value_league from history table
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.integrations.nfl_data import normalize_player_name
from backend.models.league_auction_history import LeagueAuctionHistory
from backend.models.player import Player

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSV import (manual path)
# ---------------------------------------------------------------------------

async def import_league_auction_csv(
    session: AsyncSession,
    csv_path: str | Path,
    season_year: int,
) -> dict:
    """
    Parse CSV from Yahoo Draft Recap copy-paste and import into league_auction_history.

    Supports flexible formats:
      - player_name,position,price  (minimal)
      - player_name,position,team,price  (with team)
      - Tab or comma separated
      - Rows with extra columns (takes name from col 0, price from last numeric col)

    Returns: {matched: int, unmatched: int, unmatched_names: list[str]}
    """
    path = Path(csv_path)
    raw_text = path.read_text(encoding="utf-8-sig")

    # Detect delimiter
    delimiter = "\t" if "\t" in raw_text.split("\n")[0] else ","

    rows = list(csv.reader(io.StringIO(raw_text), delimiter=delimiter))

    # Skip header row if present
    if rows and rows[0] and not _looks_like_price(rows[0][-1]):
        rows = rows[1:]

    # Load all players for matching
    result = await session.execute(select(Player))
    all_players = result.scalars().all()
    name_map: dict[str, Player] = {}
    for p in all_players:
        name_map[normalize_player_name(p.name)] = p

    matched = 0
    unmatched = 0
    unmatched_names: list[str] = []

    for row in rows:
        if not row or len(row) < 2:
            continue

        player_name = row[0].strip()
        # Find price: last column that looks numeric
        price = None
        for cell in reversed(row[1:]):
            cell = cell.strip().replace("$", "").replace(",", "")
            if cell.isdigit():
                price = int(cell)
                break

        if price is None:
            continue

        norm = normalize_player_name(player_name)
        player = name_map.get(norm)
        if not player:
            unmatched += 1
            unmatched_names.append(player_name)
            continue

        # Upsert into history table
        stmt = pg_insert(LeagueAuctionHistory).values(
            id=uuid.uuid4(),
            player_id=player.id,
            season_year=season_year,
            price=price,
            player_name=player_name,
            source="manual_csv",
        ).on_conflict_do_update(
            constraint="uq_auction_player_season_source",
            set_={"price": price, "player_name": player_name},
        )
        await session.execute(stmt)
        matched += 1

    await session.commit()
    logger.info(
        "League auction CSV import: %d matched, %d unmatched (year=%d)",
        matched, unmatched, season_year,
    )
    return {
        "matched": matched,
        "unmatched": unmatched,
        "unmatched_names": unmatched_names,
    }


# ---------------------------------------------------------------------------
# Yahoo API sync — single season (current league)
# ---------------------------------------------------------------------------

async def sync_league_auction_from_yahoo(
    session: AsyncSession,
    season_year: int,
) -> dict:
    """
    Pull draft results from Yahoo API for the current league and import.
    Uses get_draft_results_for_league() with the configured league key.
    """
    from backend.integrations.yahoo_api import (
        get_draft_results_for_league,
        get_player_details_batch,
        get_teams_in_league,
    )

    league_key = f"nfl.l.{settings.yahoo_league_id}"
    return await _sync_season(
        session, league_key, season_year,
        get_draft_results_for_league,
        get_player_details_batch,
        get_teams_in_league,
    )


# ---------------------------------------------------------------------------
# Yahoo API sync — ALL historical seasons (auto-discovery)
# ---------------------------------------------------------------------------

async def sync_all_league_history(
    session: AsyncSession,
    target_league_id: str | None = None,
) -> dict:
    """
    Auto-discover all past auction leagues via Yahoo API and pull draft results.

    1. Call get_all_user_leagues() to find all seasons
    2. Filter to auction leagues matching our YAHOO_LEAGUE_ID
    3. For each season not already synced, pull full draft data
    4. Refresh market_value_league from latest year

    Returns: {synced_seasons, total_picks, skipped_seasons, errors}
    """
    from backend.integrations.yahoo_api import (
        get_all_user_leagues,
        get_draft_results_for_league,
        get_player_details_batch,
        get_teams_in_league,
    )

    league_id = target_league_id or settings.yahoo_league_id
    if not league_id:
        return {"error": "No YAHOO_LEAGUE_ID configured", "synced_seasons": [], "total_picks": 0}

    logger.info("Starting league history sync for league_id=%s", league_id)

    # Step 1: Discover all seasons via league chain (follows renew/renewed links)
    all_leagues = await get_all_user_leagues()
    logger.info("Discovered %d total seasons via league chain", len(all_leagues))

    # All returned leagues are part of the same league chain — use them all
    matching = all_leagues

    if not matching:
        return {
            "synced_seasons": [],
            "total_picks": 0,
            "skipped_seasons": [],
            "errors": [f"No leagues found for league_id={league_id}"],
        }

    logger.info("Found %d seasons for league_id=%s", len(matching), league_id)

    # Step 3: Sync each season
    synced_seasons: list[int] = []
    skipped_seasons: list[int] = []
    errors: list[str] = []
    total_picks = 0

    for league in matching:
        season = int(league.get("season", 0))
        league_key = league.get("league_key")

        if not league_key or not season:
            continue

        # Check if already synced (>10 yahoo records = already done)
        existing = await session.execute(
            select(func.count(LeagueAuctionHistory.id))
            .where(
                LeagueAuctionHistory.season_year == season,
                LeagueAuctionHistory.source == "yahoo",
            )
        )
        count = existing.scalar() or 0
        if count > 10:
            logger.info("Season %d already synced (%d picks), skipping", season, count)
            skipped_seasons.append(season)
            continue

        try:
            result = await _sync_season(
                session, league_key, season,
                get_draft_results_for_league,
                get_player_details_batch,
                get_teams_in_league,
            )
            picks = result.get("matched", 0) + result.get("unmatched", 0)
            total_picks += picks
            synced_seasons.append(season)
            logger.info("Synced season %d: %d picks (%d matched)", season, picks, result.get("matched", 0))

            # Respect Yahoo rate limits between seasons
            await asyncio.sleep(1.0)

        except Exception as e:
            logger.error("Failed to sync season %d: %s", season, e)
            errors.append(f"Season {season}: {e}")

    # Step 4: Refresh market_value_league from latest year
    if synced_seasons:
        await refresh_market_value_league(session)

    return {
        "synced_seasons": sorted(synced_seasons),
        "total_picks": total_picks,
        "skipped_seasons": sorted(skipped_seasons),
        "errors": errors,
    }


async def _sync_season(
    session: AsyncSession,
    league_key: str,
    season_year: int,
    get_draft_fn,
    get_player_fn,
    get_teams_fn,
) -> dict:
    """
    Sync one season's draft data from Yahoo API.

    1. Get draft picks (player_key + cost + team_key)
    2. Resolve player keys to names/positions in batches of 25
    3. Get team/manager names
    4. Match to DB players where possible
    5. Upsert into league_auction_history
    """
    # Get draft picks
    picks = await get_draft_fn(league_key)
    if not picks:
        logger.warning("No draft picks returned for %s (season %d)", league_key, season_year)
        return {"matched": 0, "unmatched": 0}

    # Get player details in batches of 25
    player_keys = [p["player_key"] for p in picks if p.get("player_key")]
    player_details: dict[str, dict] = {}

    for i in range(0, len(player_keys), 25):
        batch = player_keys[i:i + 25]
        try:
            details = await get_player_fn(batch)
            for d in details:
                if d.get("player_key"):
                    player_details[d["player_key"]] = d
            # Respect Yahoo rate limits
            if i + 25 < len(player_keys):
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning("Failed to resolve player batch %d-%d: %s", i, i + 25, e)

    # Get team/manager names
    team_map: dict[str, str] = {}
    try:
        teams = await get_teams_fn(league_key)
        for t in teams:
            if t.get("team_key"):
                team_map[t["team_key"]] = t.get("team_name") or t.get("manager_name", "")
    except Exception as e:
        logger.warning("Failed to get teams for %s: %s", league_key, e)

    # Build DB player lookup: yahoo_player_id → Player, and name → Player
    result = await session.execute(select(Player))
    db_players = result.scalars().all()
    yahoo_id_map: dict[str, Player] = {}
    name_map: dict[str, Player] = {}
    for p in db_players:
        if p.yahoo_player_id:
            yahoo_id_map[p.yahoo_player_id] = p
        name_map[normalize_player_name(p.name)] = p

    matched = 0
    unmatched = 0

    for pick in picks:
        player_key = pick.get("player_key")
        cost = pick.get("cost")

        if not player_key or cost is None:
            continue

        price = int(cost)
        player_info = player_details.get(player_key, {})
        player_name = player_info.get("name", "")
        position = player_info.get("position", "")
        team_key = pick.get("team_key")
        manager_name = team_map.get(team_key, "")

        # Match by yahoo_player_id first (most reliable), then fall back to name
        db_player = yahoo_id_map.get(player_key)
        if not db_player and player_name:
            db_player = name_map.get(normalize_player_name(player_name))

        stmt = pg_insert(LeagueAuctionHistory).values(
            id=uuid.uuid4(),
            player_id=db_player.id if db_player else None,
            season_year=season_year,
            price=price,
            team_key=team_key,
            source="yahoo",
            league_key=league_key,
            yahoo_player_key=player_key,
            player_name=player_name,
            position=position,
            manager_name=manager_name,
            draft_pick_number=pick.get("pick"),
        ).on_conflict_do_update(
            constraint="uq_auction_season_source_yahoo_key",
            set_={
                "price": price,
                "player_id": db_player.id if db_player else None,
                "player_name": player_name,
                "position": position,
                "manager_name": manager_name,
                "draft_pick_number": pick.get("pick"),
            },
        )
        await session.execute(stmt)

        if db_player:
            matched += 1
        else:
            unmatched += 1

    await session.commit()
    logger.info(
        "League auction Yahoo sync: %d matched, %d unmatched (year=%d, league=%s)",
        matched, unmatched, season_year, league_key,
    )
    return {"matched": matched, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# Re-match unmatched auction history rows to players
# ---------------------------------------------------------------------------

async def rematch_unmatched_auction_history(
    session: AsyncSession,
) -> dict:
    """
    Re-match league_auction_history rows that have player_id=NULL.

    Tries to match by:
      1. yahoo_player_key → player.yahoo_player_id
      2. Normalized player_name → player.name

    Returns: {rematched: int, still_unmatched: int}
    """
    # Get all unmatched rows
    result = await session.execute(
        select(LeagueAuctionHistory)
        .where(LeagueAuctionHistory.player_id.is_(None))
    )
    unmatched_rows = result.scalars().all()

    if not unmatched_rows:
        return {"rematched": 0, "still_unmatched": 0}

    # Load all players for matching
    result = await session.execute(select(Player))
    all_players = result.scalars().all()

    # Build lookup maps
    yahoo_id_map: dict[str, Player] = {}
    name_map: dict[str, Player] = {}
    for p in all_players:
        if p.yahoo_player_id:
            yahoo_id_map[p.yahoo_player_id] = p
        key = normalize_player_name(p.name)
        if key:
            name_map[key] = p

    rematched = 0
    for row in unmatched_rows:
        matched_player = None

        # Try yahoo_player_key → yahoo_player_id
        if row.yahoo_player_key and row.yahoo_player_key in yahoo_id_map:
            matched_player = yahoo_id_map[row.yahoo_player_key]
        elif row.yahoo_player_key:
            # Try with nfl_ prefix (DB stores "nfl_" + gsis_id)
            alt_key = f"nfl_{row.yahoo_player_key}"
            if alt_key in yahoo_id_map:
                matched_player = yahoo_id_map[alt_key]

        # Fallback: name matching
        if not matched_player and row.player_name:
            norm_name = normalize_player_name(row.player_name)
            if norm_name in name_map:
                matched_player = name_map[norm_name]

        if matched_player:
            row.player_id = matched_player.id
            rematched += 1

    if rematched:
        await session.commit()

    still_unmatched = len(unmatched_rows) - rematched
    logger.info(
        "Re-matched %d auction history rows (%d still unmatched)",
        rematched, still_unmatched,
    )
    return {"rematched": rematched, "still_unmatched": still_unmatched}


# ---------------------------------------------------------------------------
# Refresh player.market_value_league from history
# ---------------------------------------------------------------------------

async def refresh_market_value_league(
    session: AsyncSession,
    season_year: int | None = None,
) -> dict:
    """
    Set player.market_value_league from the latest year in league_auction_history.
    If season_year is None, uses the most recent year in the history table.

    Returns: {updated: int, year_used: int | None}
    """
    if season_year is None:
        result = await session.execute(
            select(func.max(LeagueAuctionHistory.season_year))
        )
        season_year = result.scalar()
        if season_year is None:
            return {"updated": 0, "year_used": None}

    # Get all history records for this year that have a player_id
    result = await session.execute(
        select(LeagueAuctionHistory)
        .where(
            LeagueAuctionHistory.season_year == season_year,
            LeagueAuctionHistory.player_id.isnot(None),
        )
    )
    records = result.scalars().all()

    # Build player_id -> price mapping (prefer yahoo source over csv)
    price_map: dict[uuid.UUID, int] = {}
    for rec in records:
        existing = price_map.get(rec.player_id)
        if existing is None or rec.source == "yahoo":
            price_map[rec.player_id] = rec.price

    if not price_map:
        return {"updated": 0, "year_used": season_year}

    player_ids = list(price_map.keys())
    result = await session.execute(
        select(Player).where(Player.id.in_(player_ids))
    )
    players = result.scalars().all()

    updated = 0
    for p in players:
        p.market_value_league = Decimal(str(price_map[p.id]))
        updated += 1

    await session.commit()
    logger.info("Refreshed market_value_league for %d players (year=%d)", updated, season_year)
    return {"updated": updated, "year_used": season_year}


def _looks_like_price(value: str) -> bool:
    """Check if a string looks like a price (numeric, possibly with $ prefix)."""
    cleaned = value.strip().replace("$", "").replace(",", "")
    return cleaned.isdigit()


# ---------------------------------------------------------------------------
# Build manager/opponent profiles from auction history
# ---------------------------------------------------------------------------

_STRATEGY_LABELS = {
    "hero_rb":          "Pays premium for 1-2 elite RBs, cheap elsewhere",
    "stars_and_scrubs": "Spends big on 2-3 studs, fills rest at $1",
    "zero_rb":          "Avoids expensive RBs, invests in WR/TE/QB",
    "balanced":         "Spreads budget relatively evenly across positions",
}


def _classify_strategy(pos_pct: dict[str, float]) -> str:
    """
    Infer apparent draft strategy from positional spending percentages.

    Args:
        pos_pct: {"QB": 0.10, "RB": 0.45, "WR": 0.30, "TE": 0.05, ...}
    """
    rb_pct = pos_pct.get("RB", 0)
    wr_pct = pos_pct.get("WR", 0)
    te_pct = pos_pct.get("TE", 0)

    if rb_pct >= 0.50:
        return "hero_rb"
    if rb_pct <= 0.20 and (wr_pct >= 0.45 or te_pct >= 0.20):
        return "zero_rb"
    return "balanced"


def _classify_management_style(
    top_spend_pcts: list[float],
    budget_utilization: float,
) -> str:
    """
    Infer management style from spending concentration and budget utilization.

    Args:
        top_spend_pcts: Each year's top-2 pick spend as % of total budget.
        budget_utilization: Average % of $200 budget used across years.
    """
    avg_concentration = sum(top_spend_pcts) / len(top_spend_pcts) if top_spend_pcts else 0

    if avg_concentration >= 0.50:
        return "stars_and_scrubs"
    if budget_utilization < 0.90:
        return "conservative"
    return "analytical"


async def build_manager_profiles(
    session: AsyncSession,
    season_year: int | None = None,
) -> dict:
    """
    Build opponent profiles from league auction history spending patterns.

    Creates one OpponentProfile per manager in the latest year, populated with:
    - budget_spent / budget_remaining
    - positional_scores (spending % by position)
    - apparent_strategy (hero_rb / zero_rb / balanced)
    - management_style (stars_and_scrubs / conservative / analytical)

    Returns: {created: int, season_year: int}
    """
    from backend.models.draft_state import OpponentProfile
    from backend.utils.seasons import get_analysis_year

    if season_year is None:
        result = await session.execute(
            select(func.max(LeagueAuctionHistory.season_year))
        )
        season_year = result.scalar()
        if season_year is None:
            return {"created": 0, "error": "No auction history data"}

    analysis_year = get_analysis_year()

    # Get all picks grouped by manager for the target year
    result = await session.execute(
        select(LeagueAuctionHistory)
        .where(
            LeagueAuctionHistory.season_year == season_year,
            LeagueAuctionHistory.manager_name.isnot(None),
            LeagueAuctionHistory.manager_name != "",
        )
    )
    picks = result.scalars().all()

    if not picks:
        return {"created": 0, "error": f"No picks with manager data for {season_year}"}

    # Group picks by manager
    by_manager: dict[str, list] = {}
    team_keys: dict[str, str] = {}
    for pick in picks:
        mgr = pick.manager_name
        by_manager.setdefault(mgr, []).append(pick)
        if pick.team_key and mgr not in team_keys:
            team_keys[mgr] = pick.team_key

    # Get historical data for multi-year analysis
    result = await session.execute(
        select(LeagueAuctionHistory)
        .where(
            LeagueAuctionHistory.manager_name.isnot(None),
            LeagueAuctionHistory.manager_name != "",
        )
    )
    all_picks = result.scalars().all()

    # Group all historical picks by manager name
    history_by_manager: dict[str, list] = {}
    for pick in all_picks:
        history_by_manager.setdefault(pick.manager_name, []).append(pick)

    # Delete existing profiles for this analysis year (re-build from scratch)
    await session.execute(
        select(OpponentProfile)
        .where(OpponentProfile.season_year == analysis_year)
    )
    from sqlalchemy import delete
    await session.execute(
        delete(OpponentProfile).where(OpponentProfile.season_year == analysis_year)
    )

    created = 0
    for mgr, mgr_picks in by_manager.items():
        total_spent = sum(p.price for p in mgr_picks)
        budget_remaining = 200 - total_spent

        # Positional spending
        pos_spend: dict[str, int] = {}
        for p in mgr_picks:
            pos = p.position or "?"
            pos_spend[pos] = pos_spend.get(pos, 0) + p.price

        skill_total = sum(v for k, v in pos_spend.items() if k in ("QB", "RB", "WR", "TE"))
        pos_pct = {}
        for pos in ("QB", "RB", "WR", "TE"):
            pos_pct[pos] = round(pos_spend.get(pos, 0) / skill_total, 2) if skill_total > 0 else 0

        strategy = _classify_strategy(pos_pct)

        # Multi-year management style analysis
        # Look for this manager name in historical data
        hist = history_by_manager.get(mgr, [])
        years = set(p.season_year for p in hist)
        top_spend_pcts = []
        budget_utils = []
        for yr in years:
            yr_picks = [p for p in hist if p.season_year == yr]
            yr_total = sum(p.price for p in yr_picks)
            budget_utils.append(yr_total / 200)
            # Top 2 picks as concentration metric
            sorted_prices = sorted((p.price for p in yr_picks), reverse=True)
            top2 = sum(sorted_prices[:2])
            top_spend_pcts.append(top2 / yr_total if yr_total > 0 else 0)

        avg_util = sum(budget_utils) / len(budget_utils) if budget_utils else 1.0
        mgmt_style = _classify_management_style(top_spend_pcts, avg_util)

        # Build roster summary from picks
        roster = []
        for p in sorted(mgr_picks, key=lambda x: x.price, reverse=True):
            roster.append({
                "name": p.player_name,
                "position": p.position,
                "price": p.price,
            })

        profile = OpponentProfile(
            season_year=analysis_year,
            yahoo_team_id=team_keys.get(mgr),
            team_name=mgr,
            roster=roster,
            budget_spent=total_spent,
            budget_remaining=max(0, budget_remaining),
            positional_scores=pos_pct,
            apparent_strategy=strategy,
            management_style=mgmt_style,
        )
        session.add(profile)
        created += 1

    await session.commit()
    logger.info("Built %d manager profiles from %d auction history (year=%d)", created, season_year, analysis_year)
    return {"created": created, "season_year": analysis_year}


async def load_manager_tendencies(
    session: AsyncSession,
) -> dict[str, dict]:
    """
    Load historical manager tendencies from OpponentProfile records.

    Returns dict keyed by yahoo_team_id (or team_name fallback):
        {
            "style": "hero_rb"|"zero_rb"|"balanced",
            "management_style": "stars_and_scrubs"|"conservative"|"analytical",
            "positional_bias": {"RB": 1.3, "WR": 0.9, ...},
        }

    Positional bias is computed by comparing each manager's spending percentages
    to the league average. A value of 1.3 means 30% more spent than average.
    """
    from backend.models.draft_state import OpponentProfile

    result = await session.execute(
        select(OpponentProfile)
        .where(OpponentProfile.positional_scores.isnot(None))
        .order_by(OpponentProfile.season_year.desc())
    )
    profiles = result.scalars().all()

    if not profiles:
        return {}

    # Use most recent season_year only
    latest_year = profiles[0].season_year
    profiles = [p for p in profiles if p.season_year == latest_year]

    # Compute league-average positional spending
    league_totals: dict[str, float] = {}
    league_counts: dict[str, int] = {}
    for p in profiles:
        for pos, pct in (p.positional_scores or {}).items():
            league_totals[pos] = league_totals.get(pos, 0.0) + pct
            league_counts[pos] = league_counts.get(pos, 0) + 1

    league_avg: dict[str, float] = {
        pos: league_totals[pos] / league_counts[pos]
        for pos in league_totals
        if league_counts[pos] > 0
    }

    tendencies: dict[str, dict] = {}
    for p in profiles:
        key = p.yahoo_team_id or p.team_name
        pos_scores = p.positional_scores or {}

        # Positional bias = manager's pct / league average pct
        positional_bias: dict[str, float] = {}
        for pos, pct in pos_scores.items():
            avg = league_avg.get(pos)
            if avg and avg > 0:
                positional_bias[pos] = round(pct / avg, 2)

        tendencies[key] = {
            "style": p.apparent_strategy or "balanced",
            "management_style": p.management_style or "analytical",
            "positional_bias": positional_bias,
        }

    return tendencies
