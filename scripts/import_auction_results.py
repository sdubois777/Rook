"""Import a league's auction results from an exported text file.

WHY THIS EXISTS. The prospective backtest scores our board against what the league
ACTUALLY paid. Only one season had real prices: Yahoo's ``draftresults`` endpoint returns
no cost for older seasons (the imported 2023 rows are all $0) and some seasons do not
import at all. Without a second and third priced season, nothing measured on one season
can be confirmed — a single season carries roughly ±8 accuracy points.

This reads the league's own exported results, which DO carry prices, and writes them to
``league_auction_history`` with ``source="manual_csv"``. ``run_backtest`` prefers that
table over ``market_value_historic``, so an imported season becomes scoreable with no
further wiring.

EXPECTED FORMAT — one pick per line, tab- or space-separated::

    12.     Joe Burrow (Cin - QB)   $20     Agent Orange
    31.     Bills (Buf - DEF)       $6      Good luck Buuuuudddd

The team abbreviation in the parentheses is the player's CURRENT team, not the team he
was on that season, so it is deliberately IGNORED for skill players — only name and
position identify them. For team defences it is the only usable key (the file says
"Bills", the database says "Buffalo Bills").

IDENTITY. Name + position, and only when that pair matches EXACTLY ONE player row.
This database contains duplicate player clusters and same-name/same-position different
humans (``Frank Gore Sr`` / ``Frank Gore Jr``), so an ambiguous match is REPORTED and
skipped, never guessed — attributing a real auction price to the wrong player would
silently corrupt the very measurement this exists to enable. The canonical
``players.name`` is stored rather than the file's spelling, because the backtest joins
prices to the board BY NAME and a near-miss would drop the row.

Every unmatched line is printed. A quiet 90% match rate is how a season ends up with a
plausible-looking price list that is missing its most expensive players.

Usage::

    .venv/Scripts/python.exe scripts/import_auction_results.py \
        --season 2023 --file "C:/path/2023-Auction-Results.txt" --dry-run

    .venv/Scripts/python.exe scripts/import_auction_results.py \
        --season 2023 --file "C:/path/2023-Auction-Results.txt"
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

logging.basicConfig(level=logging.INFO, format="%(message)s")
# The dev engine echoes SQL; that would bury the match report this script exists to show.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logger = logging.getLogger("import_auction_results")

SOURCE = "manual_csv"

# "12.  Joe Burrow (Cin - QB)  $20  Agent Orange"
# The position group accepts a LIST — Yahoo exports multi-eligible players as
# "Taysom Hill (NO - QB,TE)". Matching only a single token made that line unparseable, and
# an unparseable line is a real price the backtest never sees.
_LINE = re.compile(
    r"^\s*(?P<pick>\d+)\.\s*"
    r"(?P<name>.+?)\s*"
    r"\(\s*(?P<team>[A-Za-z]+)\s*-\s*(?P<pos>[A-Za-z]+(?:\s*[,/]\s*[A-Za-z]+)*)\s*\)\s*"
    r"\$(?P<price>\d+)\s*"
    r"(?P<manager>.*?)\s*$"
)

# File team abbreviations that differ from the database's.
_TEAM_ALIASES = {
    "LAR": "LA", "LAC": "LAC", "WAS": "WAS", "JAX": "JAX", "OAK": "LV", "SD": "LAC",
    "STL": "LA", "ARZ": "ARI",
}


@dataclass(frozen=True)
class Pick:
    pick_number: int
    name: str
    team: str
    position: str          # the first listed position — what gets stored
    price: int
    manager: str
    positions: tuple[str, ...] = ()   # every listed position, in file order

    def candidates(self) -> tuple[str, ...]:
        return self.positions or (self.position,)


# Generational suffixes. The export carries them ("Travis Etienne Jr.") and this database
# generally does not ("Travis Etienne"), so a suffix-stripped fallback is needed \u2014 but it
# is applied ONLY when the stripped name+position still resolves to exactly one row.
# `Frank Gore Sr` and `Frank Gore Jr` are same-name, same-position, DIFFERENT humans, so a
# stripped match that hits two rows must be skipped, never picked from.
_SUFFIX = re.compile(r"\s+(jr|sr|ii|iii|iv|v)\.?\s*$", re.IGNORECASE)

# Nicknames the export uses that the database does not. Explicit and exact \u2014 never a
# substring rule: "Hollywood Brown" is Marquise Brown, but an unrelated "Hollywood
# Smothers" also exists, and a contains-match would happily conflate them.
_NAME_ALIASES = {
    "hollywoodbrown": "marquisebrown",
}


def _norm(text: str) -> str:
    """Fold accents, curly quotes and punctuation so 'Ja'Marr' matches 'Ja'Marr'."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"[^a-z0-9]+", "", text.lower())
    return text


def _strip_suffix(name: str) -> str:
    """'Travis Etienne Jr.' -> 'Travis Etienne'. Applied to raw text, before folding."""
    return _SUFFIX.sub("", name).strip()


def parse_lines(raw: str) -> tuple[list[Pick], list[str]]:
    """Parse the export. Returns (picks, unparseable_lines) — never silently drops."""
    picks: list[Pick] = []
    bad: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        m = _LINE.match(line)
        if not m:
            bad.append(line.strip())
            continue
        positions = tuple(
            p.strip().upper() for p in re.split(r"[,/]", m.group("pos")) if p.strip()
        )
        picks.append(Pick(
            pick_number=int(m.group("pick")),
            name=m.group("name").strip(),
            team=m.group("team").strip().upper(),
            position=positions[0],
            price=int(m.group("price")),
            manager=m.group("manager").strip(),
            positions=positions,
        ))
    return picks, bad


async def _build_index(db):
    """Three lookups: exact name+pos, suffix-stripped name+pos, and team+DEF."""
    from backend.models.player import Player

    rows = (await db.execute(
        select(Player.id, Player.name, Player.position, Player.team_abbr)
    )).all()
    by_name: dict[tuple[str, str], list] = {}
    by_base: dict[tuple[str, str], list] = {}
    by_def: dict[str, list] = {}
    for r in rows:
        pos = (r.position or "").upper()
        by_name.setdefault((_norm(r.name), pos), []).append(r)
        by_base.setdefault((_norm(_strip_suffix(r.name)), pos), []).append(r)
        if pos == "DEF":
            by_def.setdefault((r.team_abbr or "").upper(), []).append(r)
    return by_name, by_base, by_def


def _resolve(pick: Pick, by_name, by_base, by_def):
    """(row, reason). row is None when unmatched or ambiguous.

    Tried in descending order of confidence, and EVERY tier requires exactly one hit:
      1. exact name + position
      2. explicit nickname alias + position
      3. suffix-stripped name + position
    """
    if pick.position == "DEF":
        team = _TEAM_ALIASES.get(pick.team, pick.team)
        hits = by_def.get(team, [])
        if not hits:
            return None, "no DEF for that team"
        if len(hits) > 1:
            return None, f"ambiguous ({len(hits)} DEF rows)"
        return hits[0], "ok"

    key = _norm(pick.name)
    # Multi-eligible players ("QB,TE") are tried in the order the file lists them; this
    # database stores exactly one position per player.
    for position in pick.candidates():
        for candidate, index, label in (
            (key, by_name, "exact"),
            (_NAME_ALIASES.get(key), by_name, "alias"),
            (_norm(_strip_suffix(pick.name)), by_base, "suffix-stripped"),
        ):
            if candidate is None:
                continue
            hits = index.get((candidate, position), [])
            if len(hits) == 1:
                return hits[0], label if position == pick.position else f"{label} as {position}"
            if len(hits) > 1:
                # Never pick one — this is the Frank Gore Sr/Jr case.
                return None, f"ambiguous via {label} ({len(hits)} player rows)"
    return None, "no match"


async def run(season: int, path: Path, dry_run: bool) -> dict:
    from backend.database import AsyncSessionLocal
    from backend.models.league_auction_history import LeagueAuctionHistory
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    picks, bad = parse_lines(path.read_text(encoding="utf-8"))
    logger.info("parsed %d picks from %s", len(picks), path.name)
    for b in bad:
        logger.warning("  UNPARSEABLE: %s", b)
    if not picks:
        raise SystemExit("No picks parsed — check the file format.")

    stats = {"picks": len(picks), "matched": 0, "unmatched": 0, "ambiguous": 0,
             "written": 0, "unparseable": len(bad)}
    unresolved: list[tuple[Pick, str]] = []

    async with AsyncSessionLocal() as db:
        by_name, by_base, by_def = await _build_index(db)
        for p in picks:
            row, reason = _resolve(p, by_name, by_base, by_def)
            if row is None:
                stats["ambiguous" if reason.startswith("ambiguous") else "unmatched"] += 1
                unresolved.append((p, reason))
                continue
            stats["matched"] += 1
            if dry_run:
                continue
            await db.execute(
                pg_insert(LeagueAuctionHistory)
                .values(
                    player_id=row.id,
                    # Canonical DB spelling on purpose — the backtest joins BY NAME.
                    player_name=row.name,
                    position=row.position,
                    price=p.price,
                    manager_name=p.manager[:100],
                    draft_pick_number=p.pick_number,
                    season_year=season,
                    source=SOURCE,
                )
                .on_conflict_do_nothing()
            )
            stats["written"] += 1
        if dry_run:
            await db.rollback()
        else:
            await db.commit()

    logger.info("")
    logger.info("  matched            : %d", stats["matched"])
    logger.info("  unmatched          : %d", stats["unmatched"])
    logger.info("  ambiguous (skipped): %d", stats["ambiguous"])
    if unresolved:
        logger.info("")
        logger.info("  UNRESOLVED — every one is a price the backtest will not see:")
        for p, reason in unresolved:
            logger.info("    pick %-4d $%-3d %-28s %-4s  %s",
                        p.pick_number, p.price, p.name, p.position, reason)

    skill = [p for p in picks if p.position in ("QB", "RB", "WR", "TE")]
    logger.info("")
    logger.info("  skill-position picks (what the backtest scores): %d", len(skill))
    logger.info("  total spend: $%d over %d picks", sum(p.price for p in picks), len(picks))
    logger.info("")
    logger.info("%s", "DRY RUN — nothing written." if dry_run else f"Committed {stats['written']} rows.")
    return stats


async def main() -> None:
    ap = argparse.ArgumentParser(description="Import league auction results from a file.")
    ap.add_argument("--season", type=int, required=True, help="Season year, e.g. 2023")
    ap.add_argument("--file", required=True, help="Path to the exported results file")
    ap.add_argument("--dry-run", action="store_true", help="Report only; write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"No such file: {path}")

    if not args.dry_run:
        from backend.db_guard import guard_writes
        guard_writes("import_auction_results.py (writes league_auction_history)")

    await run(args.season, path, args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
