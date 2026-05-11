"""
Comprehensive verification of all league history + valuation systems.
Run: .venv/Scripts/python.exe scripts/verify_systems.py
"""
import asyncio
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from sqlalchemy import text
from backend.database import AsyncSessionLocal


async def run():
    async with AsyncSessionLocal() as s:

        # ===================================================================
        # VERIFICATION 1 — League History Data Quality
        # ===================================================================
        print("=" * 70)
        print("VERIFICATION 1 — League History Data Quality")
        print("=" * 70)

        r = await s.execute(text("""
            SELECT season_year, source, COUNT(*) as total_picks,
                   COUNT(DISTINCT manager_name) as managers,
                   SUM(price) as total_spent,
                   ROUND(AVG(price)::numeric, 2) as avg_price,
                   MAX(price) as highest, MIN(price) as lowest
            FROM league_auction_history
            GROUP BY season_year, source ORDER BY season_year, source
        """))
        print("\n-- Season Overview --")
        for row in r.all():
            print(f"  {row.season_year} ({row.source:10s}): {row.total_picks:3d} picks, "
                  f"{row.managers} managers, ${row.total_spent} total, "
                  f"avg=${row.avg_price}, high=${row.highest}, low=${row.lowest}")

        r = await s.execute(text("""
            SELECT season_year, player_name, position, price, manager_name
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY season_year ORDER BY price DESC
                ) as rn
                FROM league_auction_history
                WHERE source = 'yahoo'
            ) ranked
            WHERE rn <= 5
            ORDER BY season_year, price DESC
        """))
        print("\n-- Top 5 Most Expensive Picks Per Season --")
        cur_year = None
        for row in r.all():
            if row.season_year != cur_year:
                cur_year = row.season_year
                print(f"\n  {cur_year}:")
            mgr = (row.manager_name or "")[:20]
            print(f"    ${row.price:3d}  {row.player_name:30s} {row.position:4s}  ({mgr})")

        r = await s.execute(text("""
            SELECT manager_name,
                   COUNT(DISTINCT season_year) as seasons_active,
                   STRING_AGG(DISTINCT season_year::text, ', ' ORDER BY season_year::text) as seasons
            FROM league_auction_history
            WHERE source = 'yahoo'
            GROUP BY manager_name
            ORDER BY seasons_active DESC, manager_name
        """))
        print("\n-- Manager Consistency Across Years --")
        for row in r.all():
            print(f"  {row.manager_name:25s}  {row.seasons_active} seasons  ({row.seasons})")

        # ===================================================================
        # VERIFICATION 2 — Multi-Year Positional Bias
        # ===================================================================
        print("\n" + "=" * 70)
        print("VERIFICATION 2 — Multi-Year Positional Bias")
        print("=" * 70)

        r = await s.execute(text("""
            SELECT lah.season_year, lah.position,
                   ROUND(AVG(lah.price)::numeric, 1) as league_avg,
                   COUNT(*) as picks
            FROM league_auction_history lah
            WHERE lah.price > 5
            AND lah.position IN ('QB', 'RB', 'WR', 'TE')
            AND lah.source = 'yahoo'
            GROUP BY lah.season_year, lah.position
            ORDER BY lah.season_year, lah.position
        """))
        print("\n-- Avg Spend Per Position Per Season (picks > $5) --")
        for row in r.all():
            print(f"  {row.season_year} {row.position:3s}: avg=${row.league_avg:5.1f}  ({row.picks} picks)")

        r = await s.execute(text("""
            SELECT lah.season_year,
                   ROUND(AVG(lah.price)::numeric, 1) as avg_qb_price,
                   MAX(lah.price) as max_qb,
                   MIN(lah.price) as min_qb,
                   COUNT(*) as qb_count,
                   STRING_AGG(
                       lah.player_name || ' $' || lah.price::text,
                       ', ' ORDER BY lah.price DESC
                   ) FILTER (WHERE lah.price > 3) as qb_picks
            FROM league_auction_history lah
            WHERE lah.position = 'QB' AND lah.source = 'yahoo'
            GROUP BY lah.season_year
            ORDER BY lah.season_year
        """))
        print("\n-- QB Pricing Trend (all QBs) --")
        for row in r.all():
            print(f"  {row.season_year}: avg=${row.avg_qb_price}, max=${row.max_qb}, min=${row.min_qb}, count={row.qb_count}")
            if row.qb_picks:
                print(f"    Paid picks: {row.qb_picks}")

        # ===================================================================
        # VERIFICATION 3 — Manager Tendency Profiles
        # ===================================================================
        print("\n" + "=" * 70)
        print("VERIFICATION 3 — Manager Tendency Profiles")
        print("=" * 70)

        r = await s.execute(text("""
            SELECT manager_name, position,
                   COUNT(*) as picks,
                   ROUND(AVG(price)::numeric, 1) as avg_price,
                   MAX(price) as max_paid,
                   SUM(price) as total_spent
            FROM league_auction_history
            WHERE price > 3 AND source = 'yahoo'
            AND position IN ('QB', 'RB', 'WR', 'TE')
            GROUP BY manager_name, position
            ORDER BY manager_name, total_spent DESC
        """))
        print("\n-- Manager Positional Spending (picks > $3) --")
        cur_mgr = None
        for row in r.all():
            if row.manager_name != cur_mgr:
                cur_mgr = row.manager_name
                print(f"\n  {cur_mgr}:")
            print(f"    {row.position:3s}: {row.picks:2d} picks, avg=${row.avg_price:5.1f}, max=${row.max_paid}, total=${row.total_spent}")

        r = await s.execute(text("""
            WITH position_averages AS (
                SELECT season_year, position, AVG(price) as league_avg
                FROM league_auction_history
                WHERE price > 3 AND source = 'yahoo'
                AND position IN ('QB', 'RB', 'WR', 'TE')
                GROUP BY season_year, position
            )
            SELECT lah.manager_name, lah.position,
                   ROUND(AVG(lah.price)::numeric, 1) as manager_avg,
                   ROUND(AVG(pa.league_avg)::numeric, 1) as league_avg,
                   ROUND((AVG(lah.price) - AVG(pa.league_avg))::numeric, 1) as premium
            FROM league_auction_history lah
            JOIN position_averages pa ON pa.season_year = lah.season_year AND pa.position = lah.position
            WHERE lah.price > 3 AND lah.source = 'yahoo'
            AND lah.position IN ('QB', 'RB', 'WR', 'TE')
            GROUP BY lah.manager_name, lah.position
            HAVING ABS(AVG(lah.price) - AVG(pa.league_avg)) > 5
            ORDER BY ABS(AVG(lah.price) - AVG(pa.league_avg)) DESC
        """))
        print("\n-- Managers Who Consistently Over/Underpay (|premium| > $5) --")
        for row in r.all():
            direction = "OVERPAYS" if row.premium > 0 else "UNDERPAYS"
            print(f"  {row.manager_name:25s} {row.position:3s}: manager_avg=${row.manager_avg}, "
                  f"league_avg=${row.league_avg}, premium={row.premium:+.1f} ({direction})")

        # ===================================================================
        # VERIFICATION 4 — Price Anomaly Detection
        # ===================================================================
        print("\n" + "=" * 70)
        print("VERIFICATION 4 — Price Anomaly Detection (2025 vs History)")
        print("=" * 70)

        r = await s.execute(text("""
            WITH player_history AS (
                SELECT player_name, position,
                       AVG(price) as avg_price,
                       STDDEV(price) as price_stddev,
                       COUNT(*) as appearances
                FROM league_auction_history
                WHERE season_year < 2025 AND source = 'yahoo'
                GROUP BY player_name, position
                HAVING COUNT(*) >= 2
            )
            SELECT lah.player_name, lah.position,
                   lah.price as price_2025,
                   ROUND(ph.avg_price::numeric, 1) as hist_avg,
                   ROUND(ph.price_stddev::numeric, 1) as hist_stddev,
                   ph.appearances,
                   ROUND((ABS(lah.price - ph.avg_price) / NULLIF(ph.price_stddev, 0))::numeric, 2) as std_devs,
                   CASE
                       WHEN lah.price < ph.avg_price - ph.price_stddev * 1.5 THEN 'ANOMALY_LOW'
                       WHEN lah.price > ph.avg_price + ph.price_stddev * 1.5 THEN 'ANOMALY_HIGH'
                       ELSE 'normal'
                   END as anomaly_flag
            FROM league_auction_history lah
            JOIN player_history ph ON ph.player_name = lah.player_name AND ph.position = lah.position
            WHERE lah.season_year = 2025 AND lah.source = 'yahoo'
            ORDER BY COALESCE(ABS(lah.price - ph.avg_price) / NULLIF(ph.price_stddev, 0), 0) DESC
            LIMIT 15
        """))
        print("\n-- Top 15 Price Anomalies (2025 vs Prior Years) --")
        print(f"  {'Player':30s} {'Pos':4s} {'2025':>5s} {'Hist':>5s} {'StdDev':>6s} {'#Yrs':>4s} {'SDs':>5s} {'Flag'}")
        print(f"  {'-'*30} {'-'*4} {'-'*5} {'-'*5} {'-'*6} {'-'*4} {'-'*5} {'-'*12}")
        for row in r.all():
            sds = f"{row.std_devs:.1f}" if row.std_devs else "n/a"
            print(f"  {row.player_name:30s} {row.position:4s} ${row.price_2025:4d} ${row.hist_avg:5.1f} "
                  f"${row.hist_stddev or 0:5.1f}  {row.appearances:3d}  {sds:>5s} {row.anomaly_flag}")

        # ===================================================================
        # VERIFICATION 5 — System Values vs League History
        # ===================================================================
        print("\n" + "=" * 70)
        print("VERIFICATION 5 — System Values vs League History")
        print("=" * 70)

        r = await s.execute(text("""
            SELECT p.name, p.position, p.tier,
                   p.recommended_bid_ceiling as system_ceiling,
                   p.ai_bid_ceiling,
                   p.market_value_league as price_2025,
                   p.market_value_fantasypros as fp_consensus,
                   ROUND((p.recommended_bid_ceiling - p.market_value_league)::numeric, 1) as ceiling_vs_actual,
                   p.value_gap_signal
            FROM players p
            WHERE p.market_value_league IS NOT NULL
            AND p.market_value_league > 15
            ORDER BY p.market_value_league DESC
            LIMIT 25
        """))
        print("\n-- System Ceilings vs Actual 2025 Prices (players > $15) --")
        print(f"  {'Player':25s} {'Pos':4s} {'T':2s} {'Ceiling':>7s} {'AI_Ceil':>7s} {'Actual':>6s} {'FP':>6s} {'Gap':>5s} {'Signal'}")
        print(f"  {'-'*25} {'-'*4} {'-'*2} {'-'*7} {'-'*7} {'-'*6} {'-'*6} {'-'*5} {'-'*18}")
        for row in r.all():
            ceil = f"${row.system_ceiling:.0f}" if row.system_ceiling else "n/a"
            ai = f"${row.ai_bid_ceiling}" if row.ai_bid_ceiling else "n/a"
            fp = f"${row.fp_consensus:.0f}" if row.fp_consensus else "n/a"
            gap = f"{row.ceiling_vs_actual:+.0f}" if row.ceiling_vs_actual else "n/a"
            print(f"  {row.name:25s} {row.position:4s} T{row.tier or '?':1}  {ceil:>7s} {ai:>7s} ${row.price_2025:5.0f} {fp:>6s} {gap:>5s} {row.value_gap_signal or ''}")

        # ===================================================================
        # VERIFICATION 6 — AI Reasoning Quality
        # ===================================================================
        print("\n" + "=" * 70)
        print("VERIFICATION 6 — AI Reasoning Quality (Player Profiles)")
        print("=" * 70)

        r = await s.execute(text("""
            SELECT p.name, p.position, p.team_abbr, p.tier,
                   p.recommended_bid_ceiling, p.market_value_league as price_2025,
                   pp.clean_season_baseline
            FROM players p
            LEFT JOIN player_profiles pp ON p.id = pp.player_id
            WHERE p.name IN (
                'Puka Nacua', 'Jaxon Smith-Njigba', 'Christian McCaffrey',
                'Patrick Mahomes', 'Brock Bowers', 'Jahmyr Gibbs',
                'Amon-Ra St. Brown', 'Nico Collins'
            )
            ORDER BY p.market_value_league DESC NULLS LAST
        """))
        print()
        for row in r.all():
            ceil = f"${row.recommended_bid_ceiling:.0f}" if row.recommended_bid_ceiling else "n/a"
            price = f"${row.price_2025:.0f}" if row.price_2025 else "n/a"
            print(f"  {row.name} ({row.position}, {row.team_abbr}) — T{row.tier or '?'}, Ceiling={ceil}, Price2025={price}")
            if row.clean_season_baseline:
                baseline = row.clean_season_baseline
                for key in ['reasoning', 'role_this_season', 'confidence', 'key_risks']:
                    if key in baseline:
                        val = str(baseline[key])[:200]
                        print(f"    {key}: {val}")
                # Show PPR stats if present
                for key in ['receptions', 'yards', 'touchdowns', 'ppr_points', 'games']:
                    if key in baseline:
                        print(f"    {key}: {baseline[key]}")
            else:
                print(f"    [NO PROFILE DATA]")
            print()

        # ===================================================================
        # VERIFICATION 7 — Dependency Flags Working
        # ===================================================================
        print("=" * 70)
        print("VERIFICATION 7 — Dependency Flags")
        print("=" * 70)

        r = await s.execute(text("""
            SELECT p.name, p.position, p.team_abbr,
                   pd.flag_type, pd.trigger_player_name,
                   pd.trigger_condition, pd.value_impact_pct,
                   pd.confidence, LEFT(pd.reasoning, 150) as reasoning
            FROM players p
            JOIN player_dependencies pd ON p.id = pd.player_id
            WHERE p.name IN (
                'Puka Nacua', 'Jaxon Smith-Njigba',
                'Ladd McConkey', 'Davante Adams'
            )
            ORDER BY p.name, pd.flag_type
        """))
        print()
        for row in r.all():
            print(f"  {row.name} ({row.position}, {row.team_abbr})")
            print(f"    Flag: {row.flag_type}, Trigger: {row.trigger_player_name}, "
                  f"Condition: {row.trigger_condition}")
            print(f"    Impact: {row.value_impact_pct}%, Confidence: {row.confidence}")
            print(f"    Reasoning: {row.reasoning}")
            print()

        r = await s.execute(text("""
            SELECT COUNT(*) as phantom_flags
            FROM player_dependencies
            WHERE trigger_player_name IS NULL OR TRIM(trigger_player_name) = ''
        """))
        phantom = r.scalar()
        print(f"  Phantom flags (null/empty trigger_player_name): {phantom}")

        # ===================================================================
        # VERIFICATION 8 — Tier Distribution
        # ===================================================================
        print("\n" + "=" * 70)
        print("VERIFICATION 8 — Tier Distribution")
        print("=" * 70)

        r = await s.execute(text("""
            SELECT p.position, p.tier, COUNT(*) as players,
                   ROUND(MIN(p.market_value_league)::numeric, 0) as min_paid,
                   ROUND(MAX(p.market_value_league)::numeric, 0) as max_paid,
                   ROUND(AVG(p.market_value_league)::numeric, 0) as avg_paid,
                   STRING_AGG(p.name, ', ' ORDER BY p.market_value_league DESC NULLS LAST)
                       FILTER (WHERE p.market_value_league > 20) as notable_players
            FROM players p
            WHERE p.position IN ('QB','RB','WR','TE')
            AND p.market_value_league IS NOT NULL
            GROUP BY p.position, p.tier
            ORDER BY p.position, p.tier
        """))
        print()
        for row in r.all():
            notables = (row.notable_players or "")[:100]
            print(f"  {row.position} T{row.tier}: {row.players:3d} players, "
                  f"paid ${row.min_paid}-${row.max_paid} (avg ${row.avg_paid})")
            if notables:
                print(f"    Notable (>$20): {notables}")

        # ===================================================================
        # VERIFICATION 9 — Valuation Calibration Score
        # ===================================================================
        print("\n" + "=" * 70)
        print("VERIFICATION 9 — Valuation Calibration Score")
        print("=" * 70)

        r = await s.execute(text("""
            SELECT p.position, COUNT(*) as players,
                   ROUND(AVG(ABS(p.recommended_bid_ceiling - p.market_value_league))::numeric, 1) as mae,
                   ROUND(AVG(p.recommended_bid_ceiling - p.market_value_league)::numeric, 1) as mean_bias,
                   COUNT(CASE WHEN p.recommended_bid_ceiling > p.market_value_league * 1.15 THEN 1 END) as too_high,
                   COUNT(CASE WHEN p.recommended_bid_ceiling < p.market_value_league * 0.85 THEN 1 END) as too_low,
                   COUNT(CASE WHEN ABS(p.recommended_bid_ceiling - p.market_value_league) <= p.market_value_league * 0.15 THEN 1 END) as within_15pct
            FROM players p
            WHERE p.market_value_league > 10
            AND p.recommended_bid_ceiling IS NOT NULL
            GROUP BY p.position
            ORDER BY p.position
        """))
        print(f"\n  {'Pos':4s} {'N':>3s} {'MAE':>6s} {'Bias':>6s} {'High':>5s} {'Low':>5s} {'In15%':>5s} {'%In15':>5s}")
        print(f"  {'-'*4} {'-'*3} {'-'*6} {'-'*6} {'-'*5} {'-'*5} {'-'*5} {'-'*5}")
        for row in r.all():
            pct = f"{row.within_15pct/row.players*100:.0f}%" if row.players else "n/a"
            print(f"  {row.position:4s} {row.players:3d} ${row.mae:5.1f} {row.mean_bias:+5.1f} "
                  f"{row.too_high:5d} {row.too_low:5d} {row.within_15pct:5d} {pct:>5s}")

        # ===================================================================
        # VERIFICATION 10 — Live Draft Readiness
        # ===================================================================
        print("\n" + "=" * 70)
        print("VERIFICATION 10 — Live Draft Readiness")
        print("=" * 70)

        r = await s.execute(text("""
            SELECT COUNT(*) as total_players,
                   COUNT(market_value_league) as has_league_price,
                   COUNT(market_value_fantasypros) as has_fp_price,
                   COUNT(market_value) as has_market_value,
                   COUNT(recommended_bid_ceiling) as has_ceiling,
                   COUNT(ai_bid_ceiling) as has_ai_ceiling,
                   COUNT(auction_note) as has_auction_note
            FROM players
            WHERE position IN ('QB','RB','WR','TE')
        """))
        print("\n-- Market Value Coverage --")
        row = r.one()
        print(f"  Total skill players:    {row.total_players}")
        print(f"  Has league price:       {row.has_league_price}")
        print(f"  Has FP consensus:       {row.has_fp_price}")
        print(f"  Has market_value:       {row.has_market_value}")
        print(f"  Has bid ceiling:        {row.has_ceiling}")
        print(f"  Has AI bid ceiling:     {row.has_ai_ceiling}")
        print(f"  Has auction note:       {row.has_auction_note}")

        # Opponent profiles check
        r = await s.execute(text("""
            SELECT ah.manager_name, op.id IS NOT NULL as has_profile
            FROM (
                SELECT DISTINCT manager_name
                FROM league_auction_history
                WHERE season_year = 2025 AND source = 'yahoo'
                AND manager_name IS NOT NULL AND manager_name != ''
            ) ah
            LEFT JOIN opponent_profiles op ON op.team_name = ah.manager_name
            ORDER BY ah.manager_name
        """))
        print("\n-- Manager -> Opponent Profile Mapping --")
        for row in r.all():
            status = "LINKED" if row.has_profile else "NO PROFILE"
            print(f"  {row.manager_name:25s}  {status}")


asyncio.run(run())
