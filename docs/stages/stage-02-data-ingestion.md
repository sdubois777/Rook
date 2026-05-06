# Stage 2: NFL Data Ingestion Layer

## Before starting, read:
- `docs/rules/COST_RULES.md`
- `docs/rules/PATTERNS.md`
- `docs/ARCHITECTURE.md`

---

## Goal
All NFL data sources accessible as clean Python functions.
All agents read from these wrappers — never from raw data sources directly.
Data cached locally so agents don't re-fetch on every run.

---

## Model
No AI calls in this stage. Pure Python data fetching and caching.

---

## Tasks

### 1. seasons.py verification
Before writing any integration code, verify `backend/utils/seasons.py` exists
and all agents will use it. Run:
```bash
python -c "from backend.utils.seasons import get_analysis_seasons; print(get_analysis_seasons())"
```
Output should be last 3 seasons relative to today's date — NOT hardcoded years.

### 2. nfl_data_py wrapper
Create `backend/integrations/nfl_data.py`:

```python
from backend.utils.seasons import get_analysis_seasons, get_current_season

def get_pbp_data(season: int) -> pd.DataFrame
def get_player_stats(season: int) -> pd.DataFrame
def get_snap_counts(season: int) -> pd.DataFrame
def compute_target_share(season: int) -> pd.DataFrame
    # Returns: player_name, team, position, games, total_targets,
    #          avg_target_share, air_yards_share, targets_per_route_run
def fetch_weekly_stats(season: int) -> pd.DataFrame
def get_adp_data() -> pd.DataFrame
def get_schedule(season: int) -> pd.DataFrame
def get_injury_data(season: int) -> pd.DataFrame
def get_nfl_draft_picks(year: int) -> pd.DataFrame
    # Returns: player_name, position, round, pick_number, team,
    #          college, age_at_draft
def get_draft_capital_value(round: int, pick: int) -> float
    # Normalize round/pick to 0-100 using approximate value draft chart
    # Pick 1 = 100, Pick 32 = 74, Pick 33 = 73, ... Pick 256 = 1
    # This normalizes draft position into a single comparable number
```

All functions use `get_current_season()` or `get_analysis_seasons()` as defaults.
Never accept a hardcoded year as a default parameter.

Cache results to `data/cache/` directory (gitignored) to avoid re-fetching.
Cache key: `{function_name}_{season}.parquet`

### 3. OverTheCap scraper
Create `backend/integrations/overthecap.py`:

```python
def get_transactions(team_abbr: str, year: int) -> list[dict]
    # Returns: player_name, transaction_type, aav, date, position
def get_roster(team_abbr: str) -> list[dict]
    # Returns: player_name, position, age, contract_year, aav
def get_skill_roster_summary(team_abbr: str) -> list[dict]
    # Returns only QB/RB/WR/TE — filtered for agent use
def get_transactions_summary(team_abbr: str, year: int) -> list[dict]
    # Returns summarized transactions — pre-aggregated for prompt use
def get_contracts() -> pd.DataFrame
```

Cache scraped results for 24 hours — OverTheCap doesn't update in real time.

### 4. FantasyPros market value scraper
Create `backend/integrations/fantasypros.py`:

Uses Playwright to scrape auction values — FantasyPros has no public API.

```python
async def get_auction_values(format: str = "ppr") -> list[dict]
    # Returns: player_name, position, team, auction_value, adp
async def get_adp(format: str = "ppr") -> list[dict]
```

The scraper is implemented and wired to `scripts/refresh_market_values.py` (CLI)
and `POST /pipeline/refresh-market-values` (API). Also triggerable from the Pipeline Admin UI.
Market values should be refreshed within 72 hours of the actual draft.
FantasyPros publishes data ~July/August — running before that returns empty (no error).

### 5. Data seeding script
Create `scripts/seed_nfl_data.py`:

```python
# Pulls and caches last 3 seasons of data
# Uses get_analysis_seasons() — never hardcoded
from backend.utils.seasons import get_analysis_seasons, get_current_season

for season in get_analysis_seasons(lookback=3):
    nfl_data.get_pbp_data(season)
    nfl_data.compute_target_share(season)
    nfl_data.fetch_weekly_stats(season)
    nfl_data.get_snap_counts(season)
    nfl_data.get_injury_data(season)

# College data — covers players currently in their first 3 NFL years
# Look back 6 college seasons to capture all current NFL rookies/sophomores
college_seasons = list(range(
    get_current_season() - 6,
    get_current_season()
))
cfb_data.get_college_player_profiles(college_seasons)
cfb_data.get_college_target_share(college_seasons)
cfb_data.get_college_rushing_stats(college_seasons)

# Build historical comp table (expensive — cache aggressively)
# Only rebuild when new season data becomes available
cfb_data.build_historical_comp_table()

# Current year draft class
nfl_data.get_nfl_draft_picks(get_current_season())
```

Supports `--dry-run` flag that shows what would be fetched without fetching.

### 6. cfbfastR college data — expanded
Create `backend/integrations/cfb_data.py`:

College data serves two distinct purposes:
1. **QB/WR trust signals** — which receivers a QB has chemistry with
2. **Rookie prospect evaluation** — the foundation for projecting first-year players

**ASK USER:** Do you have R installed for cfbfastR, or should we use pre-exported
CSV files? If pre-exported, they should be placed in `data/college/` and the
wrapper will load from there. The R package approach is more flexible for future
seasons but requires R to be installed.

```python
def get_college_target_share(seasons: list[int]) -> pd.DataFrame
    # Returns: player_name, school, season, position, targets, target_share,
    #          yards_per_route_run, dominator_rating, games, receptions, yards, TDs
    #
    # dominator_rating = % of team's receiving yards + TDs the player accounted for
    # This is the single most predictive college receiving metric for NFL translation
    # Formula: (player_receiving_yards / team_receiving_yards +
    #           player_receiving_tds / team_receiving_tds) / 2

def get_college_rushing_stats(seasons: list[int]) -> pd.DataFrame
    # For RB prospects:
    # Returns: player_name, school, season, carries, yards, yards_per_carry,
    #          yards_after_contact, broken_tackle_rate,
    #          usage_rate (carries as % of team rushing attempts)

def get_qb_wr_college_connections() -> list[dict]
    # All QB/WR pairs who played together at same school
    # Pre-computed and cached — used by Roster Changes agent for trust scores

def get_college_player_profiles(seasons: list[int]) -> pd.DataFrame
    # Combined profile all positions — all relevant stats in one DataFrame
    # Used as input to the historical comp model

def get_draft_class(year: int) -> pd.DataFrame
    # All players drafted: round, pick, position, school
    # Joins to college profile data by player_name + school

def build_historical_comp_table() -> pd.DataFrame
    """
    THE KEY FUNCTION for rookie evaluation.

    For every player drafted in the last 8-10 seasons:
      - Their college production profile (dominator_rating, yards_per_route, etc.)
      - Their draft capital (round + pick → normalized 0-100 value)
      - Their actual NFL outcomes: PPR points per game in Year 1, Year 2, Year 3

    This is what lets the agent say:
    "This WR profile (42% dominator, SEC, pick 12) historically produces
     a WR2 by Year 2, with a WR1 ceiling in the best cases."

    Cache aggressively — only rebuild when a new NFL season's data is available.
    Store as: data/cache/historical_comp_table.parquet
    """
```

**Conference competition multipliers:**
College stats from weaker conferences inflate raw numbers. Apply these
multipliers to dominator_rating before comparing across prospects:

```python
CONFERENCE_MULTIPLIERS = {
    "SEC": 1.00,           # Baseline — strongest overall competition
    "Big Ten": 0.97,
    "Big 12": 0.95,
    "ACC": 0.95,
    "Pac-12": 0.93,
    "AAC": 0.85,
    "Mountain West": 0.83,
    "MAC": 0.80,
    "Sun Belt": 0.80,
    "Conference USA": 0.78,
    "Independent": 0.90,   # Notre Dame etc.
}

def get_adjusted_dominator(dominator_rating: float, conference: str) -> float:
    multiplier = CONFERENCE_MULTIPLIERS.get(conference, 0.85)
    return dominator_rating * multiplier
```

**Draft capital value normalization:**
```python
def get_draft_capital_value(round: int, pick_overall: int) -> float:
    """
    Convert draft position to normalized 0-100 value using AV-based chart.
    Pick 1 overall = 100. Pick 256 = ~1.
    Used to compare how much teams invested in a prospect.
    """
    # Approximate value chart — standard NFL draft trade value reference
    AV_CHART = {
        1: 100, 2: 96, 3: 92, 4: 88, 5: 85, 6: 82, 7: 79, 8: 76, 9: 74, 10: 72,
        # ... continues declining
        32: 48, 33: 47, 64: 28, 96: 16, 128: 9, 160: 5, 192: 3, 256: 1
    }
    # Interpolate for picks not in chart
    return AV_CHART.get(pick_overall, max(1, 100 - (pick_overall * 0.38)))

def get_capital_signal(capital_value: float) -> str:
    if capital_value >= 70: return "high"    # Rounds 1-2
    if capital_value >= 40: return "medium"  # Rounds 3-4
    return "low"                              # Rounds 5-7
```

---

## Required test cases
```python
# tests/unit/integrations/test_nfl_data.py
def test_compute_target_share_returns_expected_columns()
def test_compute_target_share_uses_dynamic_season()
def test_cache_created_after_first_fetch()
def test_cache_used_on_second_fetch()
def test_draft_picks_returns_correct_columns()
def test_draft_capital_value_pick_1_is_100()
def test_draft_capital_value_decreases_with_pick_number()
def test_draft_capital_signal_round1_is_high()
def test_draft_capital_signal_round6_is_low()

# tests/unit/integrations/test_cfb_data.py
def test_college_target_share_returns_expected_columns()
def test_dominator_rating_formula_correct()
    # (player_rec_yards / team_rec_yards + player_rec_tds / team_rec_tds) / 2
def test_conference_multiplier_sec_is_baseline()
def test_conference_multiplier_mac_reduces_dominator()
def test_adjusted_dominator_applies_conference_multiplier()
def test_historical_comp_table_has_both_college_and_nfl_outcomes()
def test_comp_table_covers_last_8_seasons()
def test_qb_wr_connections_includes_shared_college_seasons()
def test_college_season_lookback_covers_current_nfl_players()

# tests/unit/integrations/test_overthecap.py
def test_get_transactions_returns_list()
def test_get_skill_roster_filters_to_skill_positions()
def test_transactions_summary_is_compact()

# tests/unit/integrations/test_seasons_integration.py
def test_seed_script_uses_analysis_seasons_not_hardcoded()
def test_no_hardcoded_years_in_nfl_data_module()
def test_no_hardcoded_years_in_overthecap_module()
def test_no_hardcoded_years_in_cfb_data_module()
def test_college_season_range_derived_from_current_season()
```

---

## Verification before marking complete
1. `python scripts/seed_nfl_data.py --dry-run` runs without error and shows correct seasons
2. **ASK USER** to run `python scripts/seed_nfl_data.py` — verify data loads for correct seasons
3. Can query target share for a known player for a known season — data matches expected stats
4. Can query college profile for a known recent prospect (e.g. a receiver from last year's draft)
5. Historical comp table exists with both college stats AND NFL outcomes for multiple seasons
6. Draft capital value returns 100 for pick 1, decreasing values for later picks
7. No hardcoded years in any integration file
8. All unit tests passing, coverage 80%+

---

## Commit
```
feat(data-ingestion): implement NFL data integration layer

nfl_data_py, OverTheCap, cfbfastR wrappers complete.
College production stats with dominator rating and conference multipliers.
Historical comp table for rookie evaluation.
NFL draft picks and draft capital value normalization.
All season references dynamic via seasons.py.
Local parquet cache prevents redundant fetches.
Coverage: X%.
```

---

## Ask user
- Whether to run FantasyPros scraper now or defer until closer to draft
- Whether R/cfbfastR is available or to use pre-exported CSV files
- To run the seed script and confirm data loads for correct seasons
- To spot-check college profile for 2-3 known recent draft picks
- To verify historical comp table has recognizable player names
