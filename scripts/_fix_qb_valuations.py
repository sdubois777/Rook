"""One-shot: find and clear any QB rows that have bid ceilings."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from sqlalchemy import text
from backend.database import AsyncSessionLocal

async def fix():
    async with AsyncSessionLocal() as s:
        r = await s.execute(text(
            "SELECT id, name, team_abbr, position, recommended_bid_ceiling "
            "FROM players WHERE position='QB' AND recommended_bid_ceiling IS NOT NULL"
        ))
        rows = r.fetchall()
        print(f"QBs with ceilings: {len(rows)}")
        for row in rows:
            print(f"  {row[1]} ({row[2]}): ${float(row[4]):.2f}")

        if rows:
            await s.execute(text("""
                UPDATE players SET
                    tier=NULL, baseline_value=NULL, risk_adjusted_value=NULL,
                    recommended_bid_ceiling=NULL, let_go_threshold=NULL,
                    elite_anchor_weight=NULL, positional_scarcity_modifier=NULL,
                    value_gap=NULL, value_gap_signal=NULL
                WHERE position='QB' AND recommended_bid_ceiling IS NOT NULL
            """))
            await s.commit()
            print("Cleared.")
        else:
            print("Nothing to clear.")

asyncio.run(fix())
