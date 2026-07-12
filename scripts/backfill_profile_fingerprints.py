"""Backfill material-input fingerprints onto existing player profiles.

One-shot migration companion to the value-delta staleness fix: profiles written
before the fingerprint existed have no ``input_fingerprint`` in
clean_season_baseline, so they'd fall back to the legacy TIMESTAMP dirty test —
the exact behavior the fix removes (a bulk sync stamping 134 rows would mark
all 134 stale).

Rule: a profile gets a fingerprint stamped ONLY if the LEGACY logic considers
it FRESH — by legacy's own judgment it is consistent with the current inputs,
so fingerprinting it as "current" is honest. Legacy-STALE profiles are left
unfingerprinted: they genuinely need a regen, which stamps the fingerprint on
write. No LLM calls; DB-only.

Usage:
    uv run python scripts/backfill_profile_fingerprints.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("backfill_fingerprints")


async def main(dry_run: bool) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from backend.agents.player_profiles import PlayerProfilesAgent
    from backend.agents.team_systems import NFL_TEAMS
    from backend.database import AsyncSessionLocal
    from backend.models.player import Player, PlayerProfile

    agent = PlayerProfilesAgent.__new__(PlayerProfilesAgent)  # no client needed

    total_stamped = total_stale = total_had = 0
    for team in NFL_TEAMS:
        # stale set (legacy path for unfingerprinted rows) + current fingerprints
        stale, fingerprints = await PlayerProfilesAgent._get_stale_players(
            agent, team, force=False,
        )
        stale = stale or set()

        async with AsyncSessionLocal() as session:
            players = (
                await session.execute(select(Player).where(Player.team_abbr == team))
            ).scalars().all()
            by_id = {p.id: p.name for p in players}
            if not players:
                continue
            profs = (
                await session.execute(
                    select(PlayerProfile).where(
                        PlayerProfile.player_id.in_(list(by_id)),
                    )
                )
            ).scalars().all()

            stamped = 0
            for prof in profs:
                name = by_id.get(prof.player_id)
                if not name or name not in fingerprints:
                    continue
                baseline = prof.clean_season_baseline or {}
                if baseline.get("input_fingerprint"):
                    total_had += 1
                    continue
                if name in stale:
                    total_stale += 1     # genuinely stale — leave for a real regen
                    continue
                baseline["input_fingerprint"] = fingerprints[name]
                prof.clean_season_baseline = baseline
                flag_modified(prof, "clean_season_baseline")
                stamped += 1
            if not dry_run and stamped:
                await session.commit()
            total_stamped += stamped
            print(f"{team}: stamped={stamped}")

    print(
        f"\nDONE{' (dry run — nothing written)' if dry_run else ''}: "
        f"stamped={total_stamped}, already_had={total_had}, "
        f"left_stale_for_regen={total_stale}"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
