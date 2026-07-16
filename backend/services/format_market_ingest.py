"""
Per-format ADP + auction ingest (G5) — re-scraped EVERY pipeline run.

ADP (FantasyPros overall rank) and auction $ (DraftWizard) are the last per-format
INPUTS. They drift continuously before draft day (injuries, camp news, ADP movement),
so a static one-time import goes stale — ingestion MUST run live on each pass, never a
cached snapshot. This writes all three formats (ppr/half_ppr/standard) into
player_format_values alongside the value/prose/budget columns already there.

Isolation guarantees:
  * The players-table PPR paths (scripts/sync_adp.py, backend/engines/market_values.py)
    are UNTOUCHED — this ADDS per-format data, it does not alter what PPR leagues see.
  * Auction $ use the CANONICAL flex-fixed roster (see below); the divergence in the
    non-PPR within-position auction-$ model is a separate Phase 2 blocker — NOT here.

Nothing reads player_format_values on any product surface yet, so this is inert until
a Phase 2 read surface threads formats in.

Structure: a PURE matcher (build_format_market_upserts) that is heavily unit-tested,
an injectable orchestrator (ingest_format_market_data) that the re-scrape gate drives
with mocked scrapers, and a thin pipeline entry point (run_format_market_ingest_stage).
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy import func as sa_func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import AsyncSessionLocal
from backend.models.player import Player
from backend.models.player_format_values import PlayerFormatValues
from backend.scoring import SCORING_FORMATS

logger = logging.getLogger(__name__)

# --- Canonical auction roster (DECIDED — one shape for all three formats) -------------
# 12 teams, QB1/RB2/WR3/TE1/FLEX(WR/RB/TE)1/DST1/K1/BN6. The default DraftWizard URL
# omits FLEX; including flex=1 is the "flex fix" so $ reflect a real flex roster.
# Leagues with non-standard rosters get $ off THIS shape — auction_roster_shape carries
# the disclosure string so Phase 2 can surface the assumption.
CANONICAL_TEAMS = 12
CANONICAL_ROSTER: dict[str, int] = {
    "QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "DST": 1, "K": 1, "BN": 6,
}
CANONICAL_ROSTER_SHAPE = "12T:QB1/RB2/WR3/TE1/FLEX1/DST1/K1/BN6"

# FantasyPros-tuned name normalizer, mirroring scripts/sync_adp.normalize_name (the
# proven ADP matcher): strip a trailing generational suffix, map hyphen -> SPACE
# (so "Amon-Ra" == "Amon Ra"), drop periods/apostrophes, collapse whitespace. ADP and
# auction both come from FantasyPros, so the same normalization applies to both.
_SUFFIX_RE = re.compile(r"\s+(jr\.?|sr\.?|ii|iii|iv|v)$")
_PUNCT_RE = re.compile(r"[.']")
_WS_RE = re.compile(r"\s+")
_DST_ALIASES = {"DST", "DEF", "D/ST"}


def _norm(name: str | None) -> str:
    s = (name or "").lower()
    s = _SUFFIX_RE.sub("", s)
    s = s.replace("-", " ")
    s = _PUNCT_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _pos(p: str | None) -> str:
    p = (p or "").upper()
    return "DST" if p in _DST_ALIASES else p


def build_format_market_upserts(
    adp_rows: list[dict],
    auction_rows: list[dict],
    players: list,
    scoring_format: str,
    roster_shape: str,
) -> tuple[list[dict], dict]:
    """Match scraped ADP + auction rows to player records and build upsert dicts.

    PURE (no DB/IO) so it is unit-testable and re-scrape provable: given NEW scraped
    rows it returns NEW values (no internal caching). Matches by normalized name,
    disambiguating shared names by position; a still-ambiguous name is skipped (a miss
    beats a wrong-player value). Unmatched scraped rows are COUNTED and returned, never
    silently dropped. Returns (upsert_rows, summary).

    Each upsert row carries player_id + scoring_format + adp_fantasypros +
    auction_value + auction_roster_shape. A player matched by only one feed still gets a
    row (the other field stays None).
    """
    index: dict[str, list] = {}
    for p in players:
        index.setdefault(_norm(p.name), []).append(p)

    def _resolve(name: str | None, position: str | None):
        candidates = index.get(_norm(name), [])
        if len(candidates) > 1 and position:
            narrowed = [c for c in candidates if _pos(c.position) == _pos(position)]
            if narrowed:
                candidates = narrowed
        return candidates[0] if len(candidates) == 1 else None

    # player_id -> {adp, auction}. One row per player, merging both feeds.
    merged: dict = {}
    adp_matched = adp_unmatched = 0
    adp_unmatched_names: list[str] = []
    for row in adp_rows:
        rank = row.get("rank")
        if rank is None:
            continue
        player = _resolve(row.get("name"), row.get("position"))
        if player is None:
            adp_unmatched += 1
            adp_unmatched_names.append(row.get("name") or "")
            continue
        merged.setdefault(player.id, {"adp": None, "auction": None})["adp"] = float(rank)
        adp_matched += 1

    auc_matched = auc_unmatched = 0
    auc_unmatched_names: list[str] = []
    for row in auction_rows:
        value = row.get("avg_value")
        if value is None:
            continue
        player = _resolve(row.get("name"), row.get("position"))
        if player is None:
            auc_unmatched += 1
            auc_unmatched_names.append(row.get("name") or "")
            continue
        merged.setdefault(player.id, {"adp": None, "auction": None})["auction"] = float(value)
        auc_matched += 1

    upserts = [
        {
            "player_id": pid,
            "scoring_format": scoring_format,
            "adp_fantasypros": vals["adp"],
            "auction_value": vals["auction"],
            "auction_roster_shape": roster_shape if vals["auction"] is not None else None,
        }
        for pid, vals in merged.items()
    ]

    summary = {
        "scoring": scoring_format,
        "adp_matched": adp_matched,
        "adp_unmatched": adp_unmatched,
        "adp_total": sum(1 for r in adp_rows if r.get("rank") is not None),
        "auction_matched": auc_matched,
        "auction_unmatched": auc_unmatched,
        "auction_total": sum(1 for r in auction_rows if r.get("avg_value") is not None),
        "rows": len(upserts),
        "adp_unmatched_names": adp_unmatched_names[:25],
        "auction_unmatched_names": auc_unmatched_names[:25],
    }
    return upserts, summary


async def _persist_upserts(session: AsyncSession, rows: list[dict]) -> int:
    """Upsert market rows on (player_id, scoring_format), updating ONLY the ADP/auction
    columns so the value/prose columns written by other stages are never clobbered."""
    for row in rows:
        stmt = pg_insert(PlayerFormatValues).values(**row)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_player_format",
            set_={
                "adp_fantasypros": row["adp_fantasypros"],
                "auction_value": row["auction_value"],
                "auction_roster_shape": row["auction_roster_shape"],
                "updated_at": sa_func.now(),
            },
        )
        await session.execute(stmt)
    return len(rows)


async def ingest_format_market_data(
    session: AsyncSession,
    *,
    adp_scraper,
    auction_scraper,
    teams: int = CANONICAL_TEAMS,
    roster: dict[str, int] | None = None,
    roster_shape: str = CANONICAL_ROSTER_SHAPE,
    persist_fn=_persist_upserts,
) -> dict:
    """Re-scrape all three formats and upsert into player_format_values.

    Scrapers are INJECTED (async callables) so the re-scrape gate can drive this with
    changing mocked source data and prove the second run picks up the new values — the
    scrapers are awaited fresh on every call, never memoized here. `persist_fn` is
    injectable so the gate test can record upserts without a real DB.
    """
    roster = roster if roster is not None else CANONICAL_ROSTER

    players = (await session.execute(select(Player))).scalars().all()

    per_format: dict[str, dict] = {}
    total_rows = 0
    for fmt in SCORING_FORMATS:
        adp_rows = await adp_scraper(scoring_format=fmt)
        auction_rows = await auction_scraper(
            scoring_format=fmt, teams=teams, roster=roster
        )
        upserts, summary = build_format_market_upserts(
            adp_rows, auction_rows, players, fmt, roster_shape
        )
        total_rows += await persist_fn(session, upserts)
        per_format[fmt] = summary
        logger.info(
            "format_market_ingest[%s]: ADP %d/%d matched, auction %d/%d matched, %d rows",
            fmt, summary["adp_matched"], summary["adp_total"],
            summary["auction_matched"], summary["auction_total"], summary["rows"],
        )

    await session.commit()
    return {
        "formats": per_format,
        "rows_written": total_rows,
        "roster_shape": roster_shape,
        "teams": teams,
    }


# --- Pipeline entry point -------------------------------------------------------------
# Playwright needs a ProactorEventLoop on Windows for subprocess support; the API/uvicorn
# loop is a SelectorEventLoop. Mirror market_values.py: run each scrape in a dedicated
# thread with its own event loop. The pipeline (asyncio.run) tolerates this too.
_pw_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fmt-market-pw")


def _scrape_adp_in_thread(scoring_format: str) -> list[dict]:
    from backend.integrations.fantasypros import get_adp

    loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    try:
        return loop.run_until_complete(get_adp(scoring_format=scoring_format))
    finally:
        loop.close()


def _scrape_auction_in_thread(scoring_format: str, teams: int, roster: dict) -> list[dict]:
    from backend.integrations.fantasypros import get_auction_values

    loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            get_auction_values(scoring_format=scoring_format, teams=teams, roster=roster)
        )
    finally:
        loop.close()


async def run_format_market_ingest_stage() -> dict:
    """Pipeline stage: re-scrape live FantasyPros ADP + DraftWizard auction for all three
    formats and persist to player_format_values. Own DB session; threaded Playwright."""
    loop = asyncio.get_running_loop()

    async def _adp_scraper(*, scoring_format: str) -> list[dict]:
        return await loop.run_in_executor(_pw_executor, _scrape_adp_in_thread, scoring_format)

    async def _auction_scraper(*, scoring_format: str, teams: int, roster: dict) -> list[dict]:
        return await loop.run_in_executor(
            _pw_executor, _scrape_auction_in_thread, scoring_format, teams, roster
        )

    async with AsyncSessionLocal() as session:
        return await ingest_format_market_data(
            session, adp_scraper=_adp_scraper, auction_scraper=_auction_scraper
        )
