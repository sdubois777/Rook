"""
backend/integrations/nfl_comp_builder.py

Build historical rookie comp table using only nfl_data_py — no R or cfbfastR.

Draft position IS the college profile signal (the NFL has already done the
evaluation via draft capital). We look at actual Year 1 and Year 2 PPR
performance for historical draft classes grouped by (position, capital_tier).

Output: DataFrame with columns:
    player_name, position, draft_round, pick_number, capital_tier,
    capital_value, yr1_ppg, yr2_ppg
"""
from __future__ import annotations

import logging
from functools import lru_cache

import pandas as pd

from backend.integrations import nfl_data

logger = logging.getLogger(__name__)

SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}

# Draft position tiers — maps overall pick to college_profile_grade
DRAFT_TIER_RANGES = [
    (1, 10, "elite"),
    (11, 32, "strong"),
    (33, 64, "average"),
    (65, 105, "average"),
    (106, 999, "weak"),
]


def get_draft_tier(pick_overall: int) -> str:
    """Map overall pick number to a college profile grade."""
    for lo, hi, tier in DRAFT_TIER_RANGES:
        if lo <= pick_overall <= hi:
            return tier
    return "weak"


@lru_cache(maxsize=1)
def build_comp_table(
    start_year: int = 2010,
    end_year: int | None = None,
) -> pd.DataFrame:
    """
    Build a historical comp table from nfl_data_py draft + seasonal data.

    For each skill-position draftee from start_year..end_year:
      - Pull their Year 1 and Year 2 fantasy_points_ppr from seasonal data
      - Compute PPR per game (fantasy_points_ppr / games)
      - Tag with capital_tier from pick number

    end_year defaults to the newest draft class with two completed
    seasons (Year 1 + Year 2 data both available).

    Returns a DataFrame ready for comp matching and tier-based averaging.
    """
    if end_year is None:
        from backend.utils.seasons import get_current_season
        end_year = get_current_season() - 2

    all_rows: list[dict] = []

    for draft_year in range(start_year, end_year + 1):
        try:
            picks = nfl_data.fetch_nfl_draft_picks(draft_year)
        except Exception as exc:
            logger.warning("Could not load draft picks for %d: %s", draft_year, exc)
            continue

        if picks.empty:
            continue

        # Normalize column names
        pos_col = next((c for c in ("position", "pos") if c in picks.columns), None)
        pick_col = next((c for c in ("pick", "pick_number") if c in picks.columns), None)
        round_col = next((c for c in ("round", "draft_round") if c in picks.columns), None)
        name_col = next(
            (c for c in ("pfr_player_name", "player_name", "player") if c in picks.columns),
            None,
        )
        gsis_col = next((c for c in ("gsis_id", "player_id") if c in picks.columns), None)

        if not pos_col or not pick_col or not name_col:
            logger.warning("Draft picks %d missing expected columns: %s", draft_year, list(picks.columns))
            continue

        # Filter to skill positions
        skill_picks = picks[picks[pos_col].isin(SKILL_POSITIONS)].copy()
        if skill_picks.empty:
            continue

        # Load Year 1 and Year 2 seasonal data
        yr1_season = draft_year
        yr2_season = draft_year + 1

        yr1_data = _load_seasonal_safe(yr1_season)
        yr2_data = _load_seasonal_safe(yr2_season)

        for _, pick_row in skill_picks.iterrows():
            player_name = str(pick_row.get(name_col, ""))
            position = str(pick_row[pos_col])
            pick_num = int(pick_row[pick_col])
            draft_round = int(pick_row[round_col]) if round_col else 1

            # Use gsis_id for precise matching when available
            gsis_id = str(pick_row.get(gsis_col, "")) if gsis_col else ""

            capital_value = nfl_data.get_draft_capital_value(draft_round, pick_num)
            capital_tier = get_draft_tier(pick_num)

            yr1_ppg = _get_player_ppg(yr1_data, player_name, gsis_id) if yr1_data is not None else None
            yr2_ppg = _get_player_ppg(yr2_data, player_name, gsis_id) if yr2_data is not None else None

            all_rows.append({
                "player_name": player_name,
                "position": position,
                "draft_year": draft_year,
                "draft_round": draft_round,
                "pick_number": pick_num,
                "capital_tier": capital_tier,
                "capital_value": capital_value,
                "yr1_ppg": yr1_ppg,
                "yr2_ppg": yr2_ppg,
            })

    df = pd.DataFrame(all_rows)
    logger.info("Built comp table: %d rows (%d-%d)", len(df), start_year, end_year)
    return df


def get_tier_averages(comp_table: pd.DataFrame) -> dict[tuple[str, str], dict]:
    """
    Compute average Year 1 and Year 2 PPG by (position, capital_tier).

    Returns: {(position, capital_tier): {"yr1_avg_ppg": float, "yr2_avg_ppg": float, "sample_size": int}}
    """
    if comp_table.empty:
        return {}

    result: dict[tuple[str, str], dict] = {}
    for (pos, tier), group in comp_table.groupby(["position", "capital_tier"]):
        yr1_valid = group["yr1_ppg"].dropna()
        yr2_valid = group["yr2_ppg"].dropna()
        result[(pos, tier)] = {
            "yr1_avg_ppg": round(float(yr1_valid.mean()), 2) if not yr1_valid.empty else None,
            "yr2_avg_ppg": round(float(yr2_valid.mean()), 2) if not yr2_valid.empty else None,
            "sample_size": len(group),
        }

    return result


def find_comps(
    comp_table: pd.DataFrame,
    position: str,
    pick_number: int,
    n: int = 5,
) -> list[dict]:
    """
    Find the n most similar historical draftees by position and pick proximity.

    Returns list of {name, yr1_ppg, yr2_ppg, pick_number, draft_year}.
    """
    if comp_table.empty:
        return []

    pos_df = comp_table[comp_table["position"] == position].copy()
    if pos_df.empty:
        return []

    # Sort by pick proximity
    pos_df = pos_df.copy()
    pos_df["pick_dist"] = (pos_df["pick_number"] - pick_number).abs()
    top = pos_df.nsmallest(n, "pick_dist")

    return [
        {
            "name": str(row["player_name"]),
            "yr1_ppg": round(float(row["yr1_ppg"]), 2) if pd.notna(row.get("yr1_ppg")) else None,
            "yr2_ppg": round(float(row["yr2_ppg"]), 2) if pd.notna(row.get("yr2_ppg")) else None,
            "pick_number": int(row["pick_number"]),
            "draft_year": int(row["draft_year"]),
        }
        for _, row in top.iterrows()
    ]


def grade_college_profile_by_pick(pick_number: int) -> str:
    """Grade college profile using draft position as the signal."""
    return get_draft_tier(pick_number)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_seasonal_safe(season: int) -> pd.DataFrame | None:
    """Load seasonal data, returning None if unavailable."""
    try:
        return nfl_data.fetch_seasonal_data(season)
    except Exception:
        return None


def _get_player_ppg(
    seasonal: pd.DataFrame,
    player_name: str,
    gsis_id: str = "",
) -> float | None:
    """
    Look up a player's PPR points per game from seasonal data.
    Tries gsis_id first (reliable), then falls back to name matching.
    """
    if seasonal is None or seasonal.empty:
        return None

    # Identify columns
    ppr_col = next(
        (c for c in ("fantasy_points_ppr", "ppr_points") if c in seasonal.columns),
        None,
    )
    games_col = next(
        (c for c in ("games", "g") if c in seasonal.columns),
        None,
    )
    id_col = next(
        (c for c in ("player_id", "gsis_id") if c in seasonal.columns),
        None,
    )
    name_col = next(
        (c for c in ("player_name", "player_display_name", "player") if c in seasonal.columns),
        None,
    )

    if not ppr_col or not games_col:
        return None

    row = None

    # Primary: match by gsis_id
    if gsis_id and id_col:
        matches = seasonal[seasonal[id_col] == gsis_id]
        if not matches.empty:
            row = matches.iloc[0]

    # Fallback: name matching
    if row is None and name_col and player_name:
        last = player_name.split()[-1].lower() if player_name else ""
        if last:
            matches = seasonal[seasonal[name_col].str.lower().str.contains(last, na=False)]
            if len(matches) == 1:
                row = matches.iloc[0]
            elif len(matches) > 1:
                # Try full name match
                full_matches = matches[
                    matches[name_col].str.lower().str.contains(player_name.lower(), na=False)
                ]
                if not full_matches.empty:
                    row = full_matches.iloc[0]

    if row is None:
        return None

    games = int(row[games_col]) if pd.notna(row[games_col]) else 0
    ppr = float(row[ppr_col]) if pd.notna(row[ppr_col]) else 0.0

    if games < 4:  # Minimum games threshold
        return None

    return round(ppr / games, 2)
