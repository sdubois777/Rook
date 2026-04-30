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

**ASK USER** if they want to run this now or defer until closer to draft.
Market values should be refreshed within 72 hours of the actual draft.

### 5. Data seeding script
Create `scripts/seed_nfl_data.py`:

```python
# Pulls and caches last 3 seasons of data
# Uses get_analysis_seasons() — never hardcoded
from backend.utils.seasons import get_analysis_seasons

for season in get_analysis_seasons(lookback=3):
    nfl_data.get_pbp_data(season)
    nfl_data.compute_target_share(season)
    nfl_data.fetch_weekly_stats(season)
    nfl_data.get_snap_counts(season)
    nfl_data.get_injury_data(season)
```

Supports `--dry-run` flag that shows what would be fetched without fetching.

### 6. cfbfastR college data
Create `backend/integrations/cfb_data.py`:

College target share data for QB/WR connection history.

```python
def get_college_target_share(seasons: list[int]) -> pd.DataFrame
    # Returns: player_name, school, season, targets, target_share, games
def get_qb_wr_college_connections() -> list[dict]
    # Returns all QB/WR pairs who played together at same school
    # Pre-computed from cfbfastR data
```

If cfbfastR R package is not available, pre-export to CSV and load from file.
**ASK USER** if they have R installed, or whether to use pre-exported CSV files.

---

## Required test cases
```python
# tests/unit/integrations/test_nfl_data.py
def test_compute_target_share_returns_expected_columns()
def test_compute_target_share_uses_dynamic_season()  # not hardcoded
def test_cache_created_after_first_fetch()
def test_cache_used_on_second_fetch()  # no re-fetch

# tests/unit/integrations/test_overthecap.py
def test_get_transactions_returns_list()
def test_get_skill_roster_filters_to_skill_positions()
def test_transactions_summary_is_compact()  # not raw scrape dump

# tests/unit/integrations/test_seasons_integration.py
def test_seed_script_uses_analysis_seasons_not_hardcoded()
def test_no_hardcoded_years_in_nfl_data_module()  # scan for literal years
def test_no_hardcoded_years_in_overthecap_module()
```

---

## Verification before marking complete
1. `python scripts/seed_nfl_data.py --dry-run` runs without error
2. **ASK USER** to run `python scripts/seed_nfl_data.py` — verify data loads for correct seasons
3. Can query target share for a known player for a known season — data matches expected stats
4. No hardcoded years in any integration file
5. All unit tests passing, coverage 80%+

---

## Commit
```
feat(data-ingestion): implement NFL data integration layer

nfl_data_py, OverTheCap, and cfbfastR wrappers complete.
All season references dynamic via seasons.py.
Local parquet cache prevents redundant fetches.
```

---

## Ask user
- Whether to run FantasyPros scraper now or defer
- Whether R/cfbfastR is available or to use pre-exported CSVs
- To run the seed script and confirm data looks correct for a few players
