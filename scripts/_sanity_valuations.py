"""One-shot sanity check for valuation pass results."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from sqlalchemy import text
from backend.database import AsyncSessionLocal

async def check():
    async with AsyncSessionLocal() as s:
        r = await s.execute(text("""
            SELECT name, team_abbr, position, tier,
                   baseline_value, recommended_bid_ceiling, let_go_threshold
            FROM players
            WHERE recommended_bid_ceiling IS NOT NULL
            ORDER BY recommended_bid_ceiling DESC
            LIMIT 10
        """))
        print("players -- top 10 by recommended_bid_ceiling:")
        print(f"  {'PLAYER':<25} {'TM':<4} {'POS':<4} {'TIER':<5} {'SV':<8} {'CEILING':<9} LET_GO")
        print(f"  {'-'*72}")
        for row in r.fetchall():
            flag = " *** >$80" if float(row[5]) > 80 else ""
            print(f"  {row[0]:<25} {row[1]:<4} {row[2]:<4} T{row[3]:<4} ${float(row[4]):<7.0f} ${float(row[5]):<8.2f} ${float(row[6]):.2f}{flag}")

        r2 = await s.execute(text("SELECT COUNT(*) FROM players WHERE position='QB' AND recommended_bid_ceiling IS NOT NULL"))
        print(f"\n  QBs with bid ceiling: {r2.scalar()} (expected 0)")
        r3 = await s.execute(text("SELECT COUNT(*) FROM players WHERE recommended_bid_ceiling IS NOT NULL"))
        print(f"  Total players with ceilings: {r3.scalar()}")
        r4 = await s.execute(text("SELECT tier, COUNT(*) FROM players WHERE recommended_bid_ceiling IS NOT NULL GROUP BY tier ORDER BY tier"))
        print("\n  Tier distribution:")
        for row in r4.fetchall():
            print(f"    T{row[0]}: {row[1]} players")

asyncio.run(check())
