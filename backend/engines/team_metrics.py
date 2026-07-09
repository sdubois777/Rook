"""
Deterministic Teams-page fields (Teams rework, SLICE 1).

The TeamSystem Sonnet agent EMITS scheme / pass-protection / qb_tier as free labels,
and they don't reflect reality: scheme flattens to "balanced" (0 run_heavy despite a
real 0.49–0.66 pass-rate spread), pass-protection MIS-ORDERS its own sack_rate (DEN's
best sack_rate graded below BAL's near-worst), and qb_tier compresses to "solid". Yet
the real numbers are either already stored (sack_rate, 32/32) or trivially computable
from already-ingested data (pass_rate from PBP, cpoe/air-yards from NGS).

This module REPLACES those three fields with DETERMINISTIC values computed/ranked from
the real numerics — non-metered, no Sonnet, idempotent (recomputed from source each
run; overwrites the LLM output). Pure per-value functions (fixture-injectable) + an
async pass that fetches PBP/NGS and writes the TeamSystem rows. The agent keeps running
for the fields slices 2/3 still consume; only these three stop using its output.

Thresholds are ABSOLUTE (reflect real play-calling / stat bands, not forced terciles)
and live in ONE tunable block. Slice 3 unifies grades onto the widened-bell curve — the
KEY requirement here is only that each field ORDERS correctly by its real numeric.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

import pandas as pd
from sqlalchemy import select

from backend.models.team_system import TeamSystem
from backend.utils.seasons import get_current_season

logger = logging.getLogger(__name__)

# --- tunables (ONE place) ----------------------------------------------------
# SCHEME thresholds on real neutral-ish pass rate (pass plays / (pass+run)). Real NFL:
# run-leaning teams ~0.49–0.53, pass-leaning ~0.60–0.66. Absolute bands so labels mean
# the same thing every run and teams distribute across all three.
_RUN_HEAVY_MAX = 0.53      # pass_rate <= this → run_heavy
_PASS_HEAVY_MIN = 0.60     # pass_rate >= this → pass_heavy

# PASS-PROTECTION grade on real sack_rate (sacks allowed / dropbacks; LOWER = better).
# Monotonic absolute bands (best sack_rate → best grade) — fixes the mis-order. Slice 3
# remaps to the widened bell; this just has to order right.
_SACK_RATE_GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (0.045, "B+"), (0.055, "B"), (0.065, "B-"),
    (0.075, "C+"), (0.085, "C"), (0.095, "C-"),
    (float("inf"), "D+"),
)

# QB TIER on real cpoe (completion % over expectation; HIGHER = better). Rookies keep
# "rookie" (little/no data). Absolute bands over the real ~-7..+9 spread.
_CPOE_ELITE = 2.0          # cpoe >= this → elite
_CPOE_SOLID = 0.0          # >= this → solid
_CPOE_AVERAGE = -2.0       # >= this → average, else weak


# ---------------------------------------------------------------------------
# pure per-value mappings (fixture-injectable)
# ---------------------------------------------------------------------------
def scheme_from_pass_rate(pass_rate: Optional[float]) -> Optional[str]:
    """Real pass rate → offensive scheme label. None passthrough."""
    if pass_rate is None:
        return None
    if pass_rate <= _RUN_HEAVY_MAX:
        return "run_heavy"
    if pass_rate >= _PASS_HEAVY_MIN:
        return "pass_heavy"
    return "balanced"


def pass_pro_grade_from_sack_rate(sack_rate: Optional[float]) -> Optional[str]:
    """Real sack_rate → pass-protection grade, monotonic (lower sack = better). None
    passthrough (caller loud-warns a missing numeric)."""
    if sack_rate is None:
        return None
    for ceiling, grade in _SACK_RATE_GRADE_BANDS:
        if sack_rate <= ceiling:
            return grade
    return _SACK_RATE_GRADE_BANDS[-1][1]


def qb_tier_from_cpoe(cpoe: Optional[float], *, is_rookie: bool = False) -> Optional[str]:
    """Real cpoe → qb tier. Rookies keep 'rookie' (no reliable prior data). None → None
    (caller keeps the existing value + loud-warns)."""
    if is_rookie:
        return "rookie"
    if cpoe is None:
        return None
    if cpoe >= _CPOE_ELITE:
        return "elite"
    if cpoe >= _CPOE_SOLID:
        return "solid"
    if cpoe >= _CPOE_AVERAGE:
        return "average"
    return "weak"


# ---------------------------------------------------------------------------
# real-stat computation (pure over injected frames)
# ---------------------------------------------------------------------------
def compute_pass_rates(pbp: pd.DataFrame) -> dict[str, float]:
    """{team: pass_rate} = pass plays / (pass + run) over REG offensive plays. Pure —
    inject the PBP frame."""
    if pbp is None or getattr(pbp, "empty", True):
        return {}
    df = pbp
    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    df = df[df["play_type"].isin(["pass", "run"])]
    if df.empty or "posteam" not in df.columns:
        return {}
    grp = df.groupby("posteam")["play_type"].apply(lambda x: float((x == "pass").mean()))
    return {str(t): round(v, 4) for t, v in grp.items() if t and str(t) != "nan"}


# nflverse team_abbr aliases (NGS/PBP vs Rook canonical). Rams is the known mismatch.
_TEAM_ALIASES = {"LAR": "LA", "LA": "LA", "OAK": "LV", "SD": "LAC", "STL": "LA", "WSH": "WAS"}


def _canon_team(t) -> str:
    s = str(t or "").strip().upper()
    return _TEAM_ALIASES.get(s, s)


def compute_qb_metrics(ngs_passing: pd.DataFrame) -> dict[str, tuple[float, float]]:
    """{team: (cpoe, avg_intended_air_yards)} for each team's PRIMARY passer (most
    attempts) from the season-aggregate NGS passing frame (week==0). Pure — inject."""
    if ngs_passing is None or getattr(ngs_passing, "empty", True):
        return {}
    df = ngs_passing
    if "week" in df.columns:
        df = df[df["week"] == 0]
    if df.empty or "team_abbr" not in df.columns:
        return {}
    df = df.sort_values("attempts", ascending=False).drop_duplicates("team_abbr")
    out: dict[str, tuple[float, float]] = {}
    for _, r in df.iterrows():
        team = _canon_team(r["team_abbr"])
        cpoe = r.get("completion_percentage_above_expectation")
        air = r.get("avg_intended_air_yards")
        if cpoe is not None and pd.notna(cpoe):
            out[team] = (round(float(cpoe), 2), round(float(air), 2) if air is not None and pd.notna(air) else 0.0)
    return out


# ---------------------------------------------------------------------------
# the async pass (fetches PBP/NGS; overwrites the 3 LLM fields on TeamSystem)
# ---------------------------------------------------------------------------
async def apply_team_deterministic_fields(
    db,
    *,
    stats_season: Optional[int] = None,
    pbp: Optional[pd.DataFrame] = None,
    ngs_passing: Optional[pd.DataFrame] = None,
) -> dict:
    """Overwrite oc_scheme / oc_run_pass_split_tendency / pass_protection_grade /
    qb_tier (+ the real qb_cpoe / air_yards the tier is built on) with DETERMINISTIC
    values for the latest TeamSystem season. ``pbp`` / ``ngs_passing`` are injectable
    (tests); fetched from the most-recent completed season otherwise. Loud-warns any
    team missing an expected numeric — never a silent discard."""
    if stats_season is None:
        stats_season = get_current_season() - 1   # the completed season the grades reflect

    if pbp is None:
        import nfl_data_py as nfl
        pbp = nfl.import_pbp_data([stats_season], downcast=True)   # full load, then slice (never columns=)
    if ngs_passing is None:
        from backend.integrations.nfl_data import fetch_ngs_data
        ngs_passing = fetch_ngs_data("passing", stats_season)

    pass_rates = compute_pass_rates(pbp)
    qb_metrics = compute_qb_metrics(ngs_passing)

    rows = (await db.execute(select(TeamSystem))).scalars().all()
    latest = max((r.season_year for r in rows), default=None)
    rows = [r for r in rows if r.season_year == latest]

    scheme_n = passpro_n = qbtier_n = 0
    missing_pr: list[str] = []
    missing_cpoe: list[str] = []
    for r in rows:
        team = _canon_team(r.team_abbr)

        # 1. SCHEME + real pass rate
        pr = pass_rates.get(team)
        if pr is None:
            missing_pr.append(r.team_abbr)
        else:
            r.oc_run_pass_split_tendency = Decimal(str(pr))
            r.oc_scheme = scheme_from_pass_rate(pr)
            scheme_n += 1

        # 2. PASS PROTECTION from the already-stored real sack_rate
        if r.sack_rate is None:
            logger.warning("team_metrics: %s missing sack_rate — pass_protection_grade left as-is", r.team_abbr)
        else:
            r.pass_protection_grade = pass_pro_grade_from_sack_rate(float(r.sack_rate))
            passpro_n += 1

        # 3. QB TIER from real cpoe (+ store the real cpoe/air-yards the tier is built on)
        m = qb_metrics.get(team)
        if m is None:
            missing_cpoe.append(r.team_abbr)
            # keep the existing tier; do NOT fabricate. (rookies still tier below.)
            if r.rookie_qb_flag:
                r.qb_tier = "rookie"
        else:
            cpoe, air = m
            r.qb_cpoe = Decimal(str(cpoe))
            r.qb_air_yards_per_attempt = Decimal(str(air))
            r.qb_tier = qb_tier_from_cpoe(cpoe, is_rookie=bool(r.rookie_qb_flag))
            qbtier_n += 1

    await db.commit()
    if missing_pr:
        logger.warning("team_metrics: %d team(s) missing real pass_rate — scheme left as-is: %s", len(missing_pr), missing_pr)
    if missing_cpoe:
        logger.warning("team_metrics: %d team(s) missing real cpoe — qb_tier left as-is (rookies excepted): %s", len(missing_cpoe), missing_cpoe)
    logger.info(
        "team_metrics (season %s → %d teams): scheme=%d, pass_pro=%d, qb_tier=%d written deterministically",
        stats_season, len(rows), scheme_n, passpro_n, qbtier_n,
    )
    return {
        "teams": len(rows), "scheme": scheme_n, "pass_pro": passpro_n,
        "qb_tier": qbtier_n, "missing_pass_rate": missing_pr, "missing_cpoe": missing_cpoe,
    }
