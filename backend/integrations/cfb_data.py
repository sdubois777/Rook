"""
College football data integration — cfbfastR via R subprocess.

All functions cache results to data/cache/ (gitignored) as parquet files.
R + cfbfastR must be installed: install.packages("cfbfastR")

Two purposes:
  1. QB/WR college connection trust scores (Roster Changes agent)
  2. Rookie prospect evaluation — dominator rating, historical comp table
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path

import pandas as pd

from backend.utils.seasons import get_current_season

logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
COLLEGE_DIR = Path("data/college")  # fallback CSV location

# ---------------------------------------------------------------------------
# Conference multipliers (adjust raw dominator for competition level)
# ---------------------------------------------------------------------------

CONFERENCE_MULTIPLIERS: dict[str, float] = {
    "SEC": 1.00,
    "Big Ten": 0.97,
    "Big 12": 0.95,
    "ACC": 0.95,
    "Pac-12": 0.93,
    "AAC": 0.85,
    "Mountain West": 0.83,
    "MAC": 0.80,
    "Sun Belt": 0.80,
    "Conference USA": 0.78,
    "Independent": 0.90,   # Notre Dame, etc.
}


def get_adjusted_dominator(dominator_rating: float, conference: str) -> float:
    """Apply conference competition multiplier to raw dominator rating."""
    multiplier = CONFERENCE_MULTIPLIERS.get(conference, 0.85)
    return round(dominator_rating * multiplier, 3)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    COLLEGE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(name: str) -> Path:
    _ensure_dirs()
    return CACHE_DIR / f"{name}.parquet"


def _load_or_fetch(cache_name: str, fetch_fn) -> pd.DataFrame:
    path = _cache_path(cache_name)
    if path.exists():
        logger.debug("Cache hit: %s", cache_name)
        return pd.read_parquet(path)
    logger.info("Fetching via R: %s", cache_name)
    df = fetch_fn()
    if not df.empty:
        df.to_parquet(path, index=False)
    return df


# ---------------------------------------------------------------------------
# R execution helper
# ---------------------------------------------------------------------------

def _run_r_script(r_code: str) -> pd.DataFrame:
    """
    Execute an R script that writes a JSON file, then read that JSON into
    a DataFrame. Returns empty DataFrame on error.

    The R script must write its output to the path stored in the environment
    variable OUT_FILE as JSON (jsonlite::write_json).
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        out_path = tmp.name

    wrapped = f"""
suppressPackageStartupMessages({{
    library(cfbfastR)
    library(jsonlite)
    library(dplyr)
}})
tryCatch({{
{r_code}
    write_json(result, "{out_path.replace(chr(92), '/')}", na = "null", digits = 6)
}}, error = function(e) {{
    write_json(list(), "{out_path.replace(chr(92), '/')}")
    message(paste("cfbfastR error:", conditionMessage(e)))
}})
"""
    try:
        proc = subprocess.run(
            ["Rscript", "--vanilla", "-"],
            input=wrapped,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            logger.warning("R script stderr: %s", proc.stderr[:500])

        with open(out_path) as f:
            data = json.load(f)
        return pd.DataFrame(data) if isinstance(data, list) else pd.DataFrame()
    except FileNotFoundError:
        logger.error("Rscript not found. Install R and cfbfastR: install.packages('cfbfastR')")
        return pd.DataFrame()
    except subprocess.TimeoutExpired:
        logger.error("R script timed out after 120s")
        return pd.DataFrame()
    except Exception as exc:
        logger.error("R execution failed: %s", exc)
        return pd.DataFrame()
    finally:
        Path(out_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# College target share / dominator rating
# ---------------------------------------------------------------------------

def get_college_target_share(seasons: list[int]) -> pd.DataFrame:
    """
    Returns per-player receiving stats for given college seasons.

    Columns: player_name, school, season, position, conference,
             targets, receptions, yards, tds, games,
             target_share, yards_per_route_run, dominator_rating

    dominator_rating = (player_rec_yards / team_rec_yards
                        + player_rec_tds / team_rec_tds) / 2
    """
    seasons_str = json.dumps(seasons)
    cache_name = f"college_targets_{'_'.join(str(s) for s in seasons)}"

    def fetch():
        r_code = f"""
seasons_vec <- c{tuple(seasons) if len(seasons) > 1 else f'({seasons[0]})'}
raw <- cfb_stats_season_advanced(year = seasons_vec[1], stat_type = "receiving")
all_frames <- list()
for (yr in seasons_vec) {{
    tryCatch({{
        df <- cfb_stats_season_player(year = yr, stat_category = "receiving")
        df$season <- yr
        all_frames[[length(all_frames) + 1]] <- df
    }}, error = function(e) NULL)
}}
if (length(all_frames) == 0) {{
    result <- list()
}} else {{
    combined <- bind_rows(all_frames)
    team_totals <- combined %>%
        group_by(season, team) %>%
        summarise(team_rec_yards = sum(rec_yds, na.rm = TRUE),
                  team_rec_tds   = sum(rec_td,  na.rm = TRUE),
                  .groups = "drop")
    result <- combined %>%
        left_join(team_totals, by = c("season", "team")) %>%
        mutate(
            dominator_rating = ifelse(
                team_rec_yards > 0 & team_rec_tds > 0,
                (rec_yds / team_rec_yards + rec_td / team_rec_tds) / 2,
                NA_real_
            ),
            target_share = ifelse(
                !is.null(targets) & !is.na(targets), targets / sum(targets, na.rm=TRUE), NA_real_
            )
        ) %>%
        rename(
            player_name = athlete_name,
            school = team,
            yards = rec_yds,
            tds = rec_td,
            yards_per_route_run = yards_per_rec
        ) %>%
        select(player_name, school, season, conference, targets, receptions,
               yards, tds, games, target_share, yards_per_route_run, dominator_rating) %>%
        as.data.frame()
}}
"""
        return _run_r_script(r_code)

    return _load_or_fetch(cache_name, fetch)


def get_college_rushing_stats(seasons: list[int]) -> pd.DataFrame:
    """
    Returns per-player rushing stats for RB prospects.

    Columns: player_name, school, season, conference, carries,
             yards, yards_per_carry, usage_rate
    """
    cache_name = f"college_rushing_{'_'.join(str(s) for s in seasons)}"

    def fetch():
        seasons_tup = tuple(seasons) if len(seasons) > 1 else f"({seasons[0]})"
        r_code = f"""
seasons_vec <- c{seasons_tup}
all_frames <- list()
for (yr in seasons_vec) {{
    tryCatch({{
        df <- cfb_stats_season_player(year = yr, stat_category = "rushing")
        df$season <- yr
        all_frames[[length(all_frames) + 1]] <- df
    }}, error = function(e) NULL)
}}
if (length(all_frames) == 0) {{
    result <- list()
}} else {{
    combined <- bind_rows(all_frames)
    team_totals <- combined %>%
        group_by(season, team) %>%
        summarise(team_carries = sum(car, na.rm = TRUE), .groups = "drop")
    result <- combined %>%
        left_join(team_totals, by = c("season", "team")) %>%
        mutate(
            usage_rate = ifelse(team_carries > 0, car / team_carries, NA_real_)
        ) %>%
        rename(
            player_name = athlete_name,
            school = team,
            carries = car,
            yards = rush_yds,
            yards_per_carry = ypc
        ) %>%
        select(player_name, school, season, conference, carries, yards,
               yards_per_carry, usage_rate) %>%
        as.data.frame()
}}
"""
        return _run_r_script(r_code)

    return _load_or_fetch(cache_name, fetch)


def get_qb_wr_college_connections() -> list[dict]:
    """
    All QB/WR pairs who played together at the same school.
    Pre-computed; used by Roster Changes agent for trust scores.

    Returns list of dicts: {qb_name, wr_name, school, shared_seasons}
    """
    cache_path = _cache_path("qb_wr_connections")
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        return df.to_dict(orient="records")

    current = get_current_season()
    # Look back 6 seasons to cover all current NFL players' college careers
    college_seasons = list(range(current - 6, current))

    r_code = f"""
seasons_vec <- {list(college_seasons)}
all_frames <- list()
for (yr in seasons_vec) {{
    tryCatch({{
        pass_df <- cfb_stats_season_player(year = yr, stat_category = "passing") %>%
            mutate(season = yr, role = "QB") %>%
            select(athlete_name, team, season, role)
        recv_df <- cfb_stats_season_player(year = yr, stat_category = "receiving") %>%
            mutate(season = yr, role = "WR") %>%
            select(athlete_name, team, season, role)
        all_frames[[length(all_frames) + 1]] <- bind_rows(pass_df, recv_df)
    }}, error = function(e) NULL)
}}
if (length(all_frames) == 0) {{
    result <- list()
}} else {{
    combined <- bind_rows(all_frames)
    qbs <- combined %>% filter(role == "QB") %>% rename(qb_name = athlete_name)
    wrs <- combined %>% filter(role == "WR") %>% rename(wr_name = athlete_name)
    joined <- inner_join(qbs, wrs, by = c("team", "season")) %>%
        group_by(qb_name, wr_name, school = team) %>%
        summarise(shared_seasons = n_distinct(season), .groups = "drop") %>%
        as.data.frame()
    result <- joined
}}
"""
    df = _run_r_script(r_code)
    if not df.empty:
        df.to_parquet(cache_path, index=False)
    return df.to_dict(orient="records") if not df.empty else []


def get_college_player_profiles(seasons: list[int]) -> pd.DataFrame:
    """
    Combined receiving + rushing stats for all positions across given college seasons.
    Used as input to the historical comp model.
    """
    cache_name = f"college_profiles_{'_'.join(str(s) for s in seasons)}"

    def fetch():
        receiving = get_college_target_share(seasons)
        rushing   = get_college_rushing_stats(seasons)
        if receiving.empty and rushing.empty:
            return pd.DataFrame()
        frames = [f for f in (receiving, rushing) if not f.empty]
        return pd.concat(frames, ignore_index=True, sort=False)

    path = _cache_path(cache_name)
    if path.exists():
        return pd.read_parquet(path)
    df = fetch()
    if not df.empty:
        df.to_parquet(path, index=False)
    return df


def get_draft_class(year: int) -> pd.DataFrame:
    """
    All players drafted in a given year: round, pick, position, school.
    Joins to college profile data by player_name + school.
    """
    cache_name = f"draft_class_{year}"

    def fetch():
        r_code = f"""
result <- cfb_draft_picks(year = {year}) %>%
    select(name, position, round, pick, nfl_team, school, conference) %>%
    rename(player_name = name, pick_number = pick, team = nfl_team) %>%
    mutate(season = {year}) %>%
    as.data.frame()
"""
        return _run_r_script(r_code)

    return _load_or_fetch(cache_name, fetch)


def build_historical_comp_table() -> pd.DataFrame:
    """
    THE KEY FUNCTION for rookie evaluation.

    For every player drafted in the last 8-10 seasons:
      - Their college production profile (dominator_rating, yards_per_route, etc.)
      - Their draft capital (round + pick → normalized 0-100 value)
      - Their actual NFL outcomes: PPR points per game in Year 1, Year 2, Year 3

    Cached aggressively — only rebuilds when cache is absent or stale.
    Store: data/cache/historical_comp_table.parquet
    """
    from backend.integrations import nfl_data

    cache_path = _cache_path("historical_comp_table")
    if cache_path.exists():
        logger.debug("Cache hit: historical_comp_table")
        return pd.read_parquet(cache_path)

    current = get_current_season()
    # Draft classes from 8 seasons back — these players now have NFL history
    draft_years = list(range(current - 8, current))
    # College data — one year before each draft class
    college_seasons = list(range(current - 9, current))

    logger.info("Building historical comp table (draft years %s)", draft_years)

    college_df = get_college_target_share(college_seasons)

    all_comps: list[dict] = []
    for draft_year in draft_years:
        draft_df = get_draft_class(draft_year)
        if draft_df.empty:
            continue

        for _, pick in draft_df.iterrows():
            player_name = pick.get("player_name", "")
            position    = pick.get("position", "")
            if position not in {"WR", "TE", "RB"}:
                continue

            draft_round  = int(pick.get("round", 7))
            pick_overall = int(pick.get("pick_number", 200))
            capital_val  = nfl_data.get_draft_capital_value(draft_round, pick_overall)

            # Match college profile
            college_row: dict = {}
            if not college_df.empty:
                last = player_name.split()[-1].lower() if player_name else ""
                matches = college_df[
                    college_df["player_name"].str.lower().str.contains(last, na=False)
                ] if last else pd.DataFrame()
                if not matches.empty:
                    row = matches.iloc[0]
                    college_row = {
                        "dominator_rating":   float(row.get("dominator_rating", 0) or 0),
                        "yards_per_route_run": float(row.get("yards_per_route_run", 0) or 0),
                        "conference":          str(row.get("conference", "Unknown")),
                    }

            adjusted_dom = get_adjusted_dominator(
                college_row.get("dominator_rating", 0.0),
                college_row.get("conference", "Unknown"),
            )

            # NFL outcomes — PPR per game in Year 1, 2, 3
            yr1_ppg: float | None = None
            yr2_ppg: float | None = None
            for offset, attr in [(1, "yr1_ppg"), (2, "yr2_ppg")]:
                nfl_season = draft_year + offset
                if nfl_season > current:
                    break
                try:
                    ts = nfl_data.compute_target_share(nfl_season)
                    if ts.empty:
                        continue
                    last = player_name.split()[-1].lower() if player_name else ""
                    matches = ts[ts["player_name"].str.lower().str.contains(last, na=False)]
                    if not matches.empty:
                        ppg = float(matches.iloc[0].get("ppr_per_game", 0) or 0)
                        if attr == "yr1_ppg":
                            yr1_ppg = round(ppg, 2)
                        else:
                            yr2_ppg = round(ppg, 2)
                except Exception as exc:
                    logger.debug("Could not fetch NFL season %d for %s: %s", nfl_season, player_name, exc)

            all_comps.append({
                "player_name":         player_name,
                "draft_year":          draft_year,
                "position":            position,
                "draft_round":         draft_round,
                "pick_overall":        pick_overall,
                "capital_value":       capital_val,
                "dominator_rating":    college_row.get("dominator_rating", 0.0),
                "adjusted_dominator":  adjusted_dom,
                "yards_per_route_run": college_row.get("yards_per_route_run", 0.0),
                "conference":          college_row.get("conference", "Unknown"),
                "yr1_ppg":             yr1_ppg,
                "yr2_ppg":             yr2_ppg,
            })

    if not all_comps:
        logger.warning("Historical comp table is empty — cfbfastR may not have returned data")
        return pd.DataFrame()

    df = pd.DataFrame(all_comps)
    df.to_parquet(cache_path, index=False)
    logger.info("Historical comp table built: %d records", len(df))
    return df
