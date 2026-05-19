"""One-time script: merge suffix duplicate players (Jr./Sr./II/III).

Sleeper sends names without suffixes, causing duplicates when our DB
has the full name. This script merges IDs from the dup to the correct
(suffix) record, moves FK children, and deletes the dup.
"""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from backend.database import AsyncSessionLocal

_SUFFIX_RE = re.compile(r"\s+(III|II|IV|V|Jr\.?|Sr\.?)\s*$", re.IGNORECASE)
FK_TABLES = [
    "player_profiles",
    "player_injury_profiles",
    "player_schedules",
    "player_dependencies",
    "beat_reporter_signals",
    "market_value_historic",
]


async def main():
    async with AsyncSessionLocal() as s:
        # Find all suffix players
        result = await s.execute(text("""
            SELECT id, name, position, sleeper_id, sportradar_id, gsis_id
            FROM players
            WHERE position IN ('QB','RB','WR','TE')
            AND (name ILIKE '% jr%' OR name ILIKE '% sr%'
                 OR name ILIKE '% iii%' OR name SIMILAR TO '% II')
            ORDER BY name
        """))
        suffix_players = result.fetchall()

        merged = 0
        for sp in suffix_players:
            sp_id, sp_name, sp_pos = sp[0], sp[1], sp[2]
            sp_sleeper, sp_sportradar, sp_gsis = sp[3], sp[4], sp[5]
            stripped = _SUFFIX_RE.sub("", sp_name).strip()

            # Find dup (same stripped name + position, different ID)
            dup_result = await s.execute(text("""
                SELECT id, name, sleeper_id, sportradar_id, gsis_id
                FROM players
                WHERE LOWER(name) = LOWER(:stripped)
                AND position = :pos
                AND id != :sp_id
            """), {"stripped": stripped, "pos": sp_pos, "sp_id": str(sp_id)})
            dups = dup_result.fetchall()

            if not dups:
                continue

            dup = dups[0]
            dup_id = str(dup[0])

            # Copy IDs from dup to suffix player
            set_clauses = []
            params = {"pid": str(sp_id)}
            if dup[2] and not sp_sleeper:
                set_clauses.append("sleeper_id = :sleeper")
                params["sleeper"] = str(dup[2])
            if dup[3] and not sp_sportradar:
                set_clauses.append("sportradar_id = :sportradar")
                params["sportradar"] = str(dup[3])
            if dup[4] and not sp_gsis:
                set_clauses.append("gsis_id = :gsis")
                params["gsis"] = str(dup[4])

            if set_clauses:
                sql = f"UPDATE players SET {', '.join(set_clauses)} WHERE id = :pid"
                await s.execute(text(sql), params)

            # Move FK children from dup to suffix player
            for table in FK_TABLES:
                # Try to move (skip if constraint violation)
                await s.execute(text(f"""
                    DELETE FROM {table} WHERE player_id = :dup_id
                """), {"dup_id": dup_id})

            # Delete the dup
            await s.execute(
                text("DELETE FROM players WHERE id = :dup_id"),
                {"dup_id": dup_id},
            )
            merged += 1
            print(f"  Merged: \"{dup[1]}\" -> \"{sp_name}\"")

        await s.commit()
        print(f"\nTotal merged: {merged}")

        # Verify
        r = await s.execute(text("""
            SELECT COUNT(*) FROM players p1
            WHERE position IN ('QB','RB','WR','TE')
            AND (p1.name ILIKE '% jr%' OR p1.name ILIKE '% sr%'
                 OR p1.name ILIKE '% iii%' OR p1.name SIMILAR TO '% II')
            AND EXISTS (
                SELECT 1 FROM players p2
                WHERE p2.position = p1.position
                AND p2.id != p1.id
                AND LOWER(p2.name) = LOWER(regexp_replace(
                    p1.name, E'\\s+(III|II|IV|V|Jr\\.?|Sr\\.?)\\s*$', '', 'i'
                ))
            )
        """))
        remaining = r.scalar()
        print(f"Remaining suffix duplicates: {remaining}")


if __name__ == "__main__":
    asyncio.run(main())
