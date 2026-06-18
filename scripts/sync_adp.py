"""
Sync FantasyPros ADP onto the players table (snake-draft support).

Scrapes consensus ADP via backend.integrations.fantasypros.get_adp() and writes
adp_fantasypros (FantasyPros' overall RANK, kept on the same integer scale as
adp_ai) + adp_scoring, matching players by NORMALIZED name (FantasyPros names
differ from Sleeper names in punctuation and generational suffixes).

Standalone:  python scripts/sync_adp.py [--scoring ppr|half_ppr|standard]
Also invoked by the pre-draft pipeline (before the agent phases).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

# Allow running as a standalone script (mirrors the other scripts/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402
from backend.integrations.fantasypros import get_adp  # noqa: E402
from backend.models.player import Player  # noqa: E402

logger = logging.getLogger(__name__)

_SUFFIX_RE = re.compile(r"\s+(jr\.?|sr\.?|ii|iii|iv|v)$")
_PUNCT_RE = re.compile(r"[.']")
_WS_RE = re.compile(r"\s+")


def normalize_name(name: str | None) -> str:
    """Python mirror of the frontend normalizeName (utils/names.js).

    Strip a trailing generational suffix, map hyphen -> SPACE (not delete, so
    "Amon-Ra" == "Amon Ra"), drop periods/apostrophes, collapse whitespace.
    Must stay in lockstep with the frontend so name matching is consistent.
    """
    s = (name or "").lower()
    s = _SUFFIX_RE.sub("", s)          # suffix first, while "jr." is intact
    s = s.replace("-", " ")
    s = _PUNCT_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def apply_adp(adp_data: list[dict], players: list, scoring_format: str) -> dict:
    """Match ADP rows to players by normalized name and stamp adp fields.

    Pure (no DB/IO) so it is unit-testable: mutates the passed player objects
    (sets adp_fantasypros + adp_scoring) and returns a summary. When several
    players share a normalized name, disambiguate by position; skip if still
    ambiguous (better a miss than a wrong-player ADP).
    """
    index: dict[str, list] = {}
    for p in players:
        index.setdefault(normalize_name(p.name), []).append(p)

    matched = missed = 0
    for row in adp_data:
        # Store FantasyPros' overall RANK (1, 2, 3...), not the AVG ADP (1.5,
        # 3.0...). adp_ai is an overall rank too, so keeping both on the same
        # integer scale makes the board's DIFF a clean pick difference
        # (fp_rank - ai_rank) instead of comparing a rank against an average.
        rank = row.get("rank")
        if rank is None:
            continue
        candidates = index.get(normalize_name(row.get("name")), [])
        if len(candidates) > 1 and row.get("position"):
            pos = row["position"].upper()
            narrowed = [c for c in candidates if (c.position or "").upper() == pos]
            if narrowed:
                candidates = narrowed
        if len(candidates) == 1:
            player = candidates[0]
            player.adp_fantasypros = float(rank)
            player.adp_scoring = scoring_format
            matched += 1
        else:
            missed += 1
            logger.debug(
                "ADP no unique match: %s (%d candidates)",
                row.get("name"), len(candidates),
            )

    return {
        "matched": matched,
        "missed": missed,
        "scoring": scoring_format,
        "total": len(adp_data),
    }


async def sync_adp(scoring_format: str = "ppr") -> dict:
    """Fetch FantasyPros ADP and persist onto the players table."""
    adp_data = await get_adp(scoring_format=scoring_format)

    async with AsyncSessionLocal() as db:
        players = (await db.execute(select(Player))).scalars().all()
        summary = apply_adp(adp_data, players, scoring_format)
        await db.commit()

    logger.info("sync_adp complete: %s", summary)
    return summary


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Sync FantasyPros ADP onto players")
    parser.add_argument(
        "--scoring", default="ppr", choices=["ppr", "half_ppr", "standard"]
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    result = await sync_adp(args.scoring)
    print(f"[sync_adp] {result}")


if __name__ == "__main__":
    asyncio.run(_main())
