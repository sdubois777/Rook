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
from backend.utils.seasons import latest_season_with_data

logger = logging.getLogger(__name__)

# --- tunables (ONE place) ----------------------------------------------------
# SCHEME thresholds on real neutral-ish pass rate (pass plays / (pass+run)). Real NFL:
# run-leaning teams ~0.49–0.53, pass-leaning ~0.60–0.66. Absolute bands so labels mean
# the same thing every run and teams distribute across all three.
_RUN_HEAVY_MAX = 0.53      # pass_rate <= this → run_heavy
_PASS_HEAVY_MIN = 0.60     # pass_rate >= this → pass_heavy

# ───────────────────────── WIDENED-BELL CURVE (slice 3) ──────────────────────
# ONE relative/percentile→grade curve applied to EVERY graded field (pass-pro on
# sack_rate, run-block on stuff_rate, qb on cpoe, system on the composite). Full A–F
# scale with a DENSE MIDDLE — over 32 teams ≈ top 10% A, next 20% B, mid 40% C, next
# 20% D, bottom 10% F (≈3/6/13/6/3). Uses the tails that are REAL; does NOT manufacture
# mid-pack separation (13 teams sharing "C" = the true average, by design). ``pct`` is
# the fraction of the OTHER teams a value beats (1.0 = best). Tunable in ONE place.
_BELL_GRADE_CUTS: tuple[tuple[float, str], ...] = (
    (0.90, "A"), (0.70, "B"), (0.30, "C"), (0.10, "D"), (-1.0, "F"),
)
# Same curve mapped to the qb-tier vocabulary (elite/solid/average/weak). Rookies keep
# "rookie" (unreliable prior data) — excepted, not bell-ranked.
_BELL_TIER_CUTS: tuple[tuple[float, str], ...] = (
    (0.90, "elite"), (0.70, "solid"), (0.30, "average"), (-1.0, "weak"),
)
# QB-VALUE sub-composite (the accuracy fix): a WEIGHTED blend of the QB-value stats'
# percentile ranks → the number the widened bell tiers on. EPA-heavy; fantasy PPG
# carries RUSHING production (what cpoe misses for mobile QBs); success = consistency.
# cpoe is intentionally NOT here — it's a narrow accuracy stat, kept for display only.
_QB_VALUE_WEIGHTS = {"epa": 0.45, "fppg": 0.35, "success": 0.20}

# QB-RUSHING de-confound. The blend above is passing-CENTRIC: EPA/dropback and success
# rate (0.65 combined) give a QB ZERO credit for his LEGS — designed-run value is not a
# dropback, so a rushing QB's production is systematically undervalued (Allen was #1 in
# fantasy PPG yet graded only "solid"). This is an ADDITIVE bonus on the QB's rushing-
# production percentile (rush fantasy PPG): a pocket passer has ~0 rushing → ~0 bonus and
# does NOT move; a genuine rusher is lifted by exactly the rushing he's now credited for.
# Additive (not a reweight) so it never penalises non-rushers, and tier cuts are fixed
# (not a re-bell) so one QB's rise never pushes another down. NOT tuned to any team target.
_QB_RUSH_BONUS_WEIGHT = 0.15

# SYSTEM/OVERALL composite weights on the component percentiles (all higher=better after
# direction-correction). Football is QB-DRIVEN — QB dominates (the reweight fix: flat
# averaging graded KC below CHI). Scheme/personnel are situational, not quality axes, so
# they're excluded from the quality composite.
_SYSTEM_WEIGHTS = {"qb": 0.55, "pass_pro": 0.25, "run_block": 0.20}

# RED-ZONE philosophy thresholds (from real RZ play distribution).
_RZ_RUN_HEAVY = 0.55       # RZ run share >= this → "rb" (pound it)
_RZ_WR_DOMINANT = 0.55     # WR share of RZ pass targets >= this → "wr1"


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


# ───────────────────────── the widened-bell mapper ──────────────────────────
def bell_rank(value: Optional[float], all_values, *, lower_is_better: bool) -> Optional[float]:
    """Percentile rank of ``value`` among the league's ``all_values`` — the fraction of
    OTHER teams it beats (1.0 = best, 0.0 = worst). Direction-corrected so the best team
    always ranks highest. Ties share a rank. None (or <2 values) → None."""
    vals = [v for v in all_values if v is not None]
    if value is None or len(vals) < 2:
        return None
    if lower_is_better:
        worse = sum(1 for v in vals if v > value)      # teams with a HIGHER (worse) value
    else:
        worse = sum(1 for v in vals if v < value)      # teams with a LOWER (worse) value
    return worse / (len(vals) - 1)


def grade_from_pct(pct: Optional[float]) -> Optional[str]:
    """Widened-bell percentile → letter grade (A–F, dense middle). None passthrough."""
    if pct is None:
        return None
    for cut, grade in _BELL_GRADE_CUTS:
        if pct >= cut:
            return grade
    return _BELL_GRADE_CUTS[-1][1]


def tier_from_pct(pct: Optional[float], *, is_rookie: bool = False) -> Optional[str]:
    """Widened-bell percentile → qb tier (same curve as grade_from_pct). Rookies keep
    'rookie' (unreliable prior data), excepted from the bell."""
    if is_rookie:
        return "rookie"
    if pct is None:
        return None
    for cut, tier in _BELL_TIER_CUTS:
        if pct >= cut:
            return tier
    return _BELL_TIER_CUTS[-1][1]


def qb_value_pct(epa_pct, fppg_pct, success_pct, rush_pct=None) -> Optional[float]:
    """Weighted blend of the QB-value stats' percentile ranks → the number the widened
    bell tiers on. EPA-heavy; fppg carries rushing; success = consistency. A missing
    component is dropped and the remaining weights renormalise (never fabricated).

    ``rush_pct`` (the QB's rushing-production percentile) applies the ADDITIVE rushing
    de-confound: the passing-centric blend is lifted by ``_QB_RUSH_BONUS_WEIGHT * rush_pct``
    and clamped to 1.0. A pocket passer (rush_pct ≈ 0) is left unchanged; a rusher is
    credited for his legs. None ⇒ no bonus (backward-compatible)."""
    parts = [(_QB_VALUE_WEIGHTS["epa"], epa_pct),
             (_QB_VALUE_WEIGHTS["fppg"], fppg_pct),
             (_QB_VALUE_WEIGHTS["success"], success_pct)]
    parts = [(w, p) for w, p in parts if p is not None]
    if not parts:
        return None
    wsum = sum(w for w, _ in parts)
    base = sum(w * p for w, p in parts) / (wsum or 1.0)
    if rush_pct is not None:
        base = min(1.0, base + _QB_RUSH_BONUS_WEIGHT * rush_pct)
    return base


def system_composite_pct(pass_pct, run_pct, qb_pct) -> Optional[float]:
    """Weighted composite of the three component percentiles (higher=better) → a single
    0–1 system score. Missing components are dropped and the remaining weights
    renormalised (never fabricate a component)."""
    parts = [(_SYSTEM_WEIGHTS["qb"], qb_pct),
             (_SYSTEM_WEIGHTS["pass_pro"], pass_pct),
             (_SYSTEM_WEIGHTS["run_block"], run_pct)]
    parts = [(w, p) for w, p in parts if p is not None]
    if not parts:
        return None
    wsum = sum(w for w, _ in parts)
    return sum(w * p for w, p in parts) / (wsum or 1.0)


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


def compute_run_block_stuff_rate(pbp: pd.DataFrame) -> dict[str, float]:
    """{team: stuff_rate} = fraction of REG run plays tackled at/behind the LOS
    (nflverse ``tackled_for_loss``). The best OL-isolating run-block proxy available
    in standard PBP. Pure — inject the frame."""
    if pbp is None or getattr(pbp, "empty", True):
        return {}
    if "tackled_for_loss" not in pbp.columns or "posteam" not in pbp.columns:
        return {}
    df = pbp
    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    df = df[(df["play_type"] == "run") & df["tackled_for_loss"].notna()]
    if df.empty:
        return {}
    grp = df.groupby("posteam")["tackled_for_loss"].apply(lambda x: float((x == 1).mean()))
    return {str(t): round(v, 4) for t, v in grp.items() if t and str(t) != "nan"}


_PERSONNEL_RB = None  # (compiled lazily to avoid a module-level re import cost)


def compute_base_personnel(pbp: pd.DataFrame) -> dict[str, str]:
    """{team: base personnel shorthand ("11"/"12"/...)} = the team's most-used
    (RB-count, TE-count) grouping from nflverse ``offense_personnel`` over REG
    offensive plays. Real usage (honestly ~"11" for most teams — that IS modern NFL).
    Pure — inject the frame."""
    import re

    if pbp is None or getattr(pbp, "empty", True) or "offense_personnel" not in pbp.columns:
        return {}
    df = pbp
    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    df = df[df["offense_personnel"].notna() & df["posteam"].notna()]
    if df.empty:
        return {}

    def _shorthand(p) -> Optional[str]:
        if not isinstance(p, str):
            return None
        rb = re.search(r"(\d+)\s*RB", p)
        te = re.search(r"(\d+)\s*TE", p)
        return f"{rb.group(1) if rb else 0}{te.group(1) if te else 0}"

    df = df.assign(_sh=df["offense_personnel"].map(_shorthand))
    df = df[df["_sh"].notna()]
    out: dict[str, str] = {}
    for team, grp in df.groupby("posteam"):
        mode = grp["_sh"].value_counts()
        if len(mode):
            out[str(team)] = str(mode.idxmax())
    return out


def compute_red_zone_philosophy(pbp: pd.DataFrame) -> dict[str, str]:
    """{team: RZ weapon ("rb"/"te"/"wr1"/"spread")} from real red-zone (yardline_100
    <= 20) run/pass + receiver-position distribution. Pure — inject the frame."""
    if pbp is None or getattr(pbp, "empty", True):
        return {}
    df = pbp
    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    if "yardline_100" not in df.columns or "posteam" not in df.columns:
        return {}
    rz = df[(df["yardline_100"] <= 20) & (df["yardline_100"] > 0)
            & df["play_type"].isin(["pass", "run"])]
    if rz.empty:
        return {}
    out: dict[str, str] = {}
    for team, grp in rz.groupby("posteam"):
        run_share = float((grp["play_type"] == "run").mean())
        if run_share >= _RZ_RUN_HEAVY:
            out[str(team)] = "rb"
            continue
        passes = grp[grp["play_type"] == "pass"]
        pos = (passes["receiver_position"].value_counts(normalize=True)
               if "receiver_position" in passes.columns else pd.Series(dtype=float))
        te = float(pos.get("TE", 0.0))
        wr = float(pos.get("WR", 0.0))
        if te >= max(wr, 0.35):
            out[str(team)] = "te"
        elif wr >= _RZ_WR_DOMINANT:
            out[str(team)] = "wr1"
        else:
            out[str(team)] = "spread"
    return out


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


def compute_qb_value(pbp: pd.DataFrame) -> dict[str, tuple[float, float, float, float]]:
    """{team: (epa_per_dropback, success_rate, fantasy_ppg, rush_fppg)} for each team's
    PRIMARY passer, from PBP. The ACCURACY fix (recon): cpoe alone graded Mahomes/Jackson F.
    fantasy_ppg = passing + RUSHING production; ``rush_fppg`` is the RUSHING slice alone
    (rush yds*0.1 + rush TD*6, per game) — the input for the additive rushing de-confound
    (:func:`qb_value_pct`), which credits mobile QBs (Jackson/Hurts/Allen) for their legs
    without touching pocket passers. Pure — inject."""
    if pbp is None or getattr(pbp, "empty", True):
        return {}
    df = pbp
    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    if "passer_player_name" not in df.columns or "posteam" not in df.columns:
        return {}
    df = df.copy()
    for c in ("passing_yards", "pass_touchdown", "interception", "rushing_yards",
              "rush_touchdown", "fumble_lost", "qb_epa", "success"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    dbk = df[df["qb_dropback"] == 1] if "qb_dropback" in df.columns else df[df["play_type"].isin(["pass", "run"])]
    dbk = dbk[dbk["passer_player_name"].notna()]
    if dbk.empty:
        return {}
    by_qb = dbk.groupby("passer_player_name").agg(
        epa=("qb_epa", "mean"), succ=("success", "mean"),
        team=("posteam", "first"), dropbacks=("game_id", "count"), g=("game_id", "nunique"),
    )
    # fantasy production (passing by passer + rushing by that same name).
    pas = df.groupby("passer_player_name").agg(
        py=("passing_yards", "sum"), ptd=("pass_touchdown", "sum"), inte=("interception", "sum"),
    )
    rus = df.groupby("rusher_player_name").agg(
        ry=("rushing_yards", "sum"), rtd=("rush_touchdown", "sum"),
    )
    fum = df.groupby("passer_player_name")["fumble_lost"].sum() if "fumble_lost" in df.columns else None
    q = by_qb.join(pas, how="left").join(rus, how="left").fillna(0.0)
    if fum is not None:
        q = q.join(fum.rename("fl"), how="left").fillna({"fl": 0.0})
    else:
        q["fl"] = 0.0
    q = q[q["g"] >= 6]                                   # need a real sample
    if q.empty:
        return {}
    q["fppg"] = (q["py"] * 0.04 + q["ptd"] * 4 - q["inte"] * 2
                 + q["ry"] * 0.1 + q["rtd"] * 6 - q["fl"] * 2) / q["g"]
    q["rush_fppg"] = (q["ry"] * 0.1 + q["rtd"] * 6) / q["g"]   # rushing slice for the de-confound
    q = q.sort_values("dropbacks", ascending=False).reset_index()
    q = q.drop_duplicates("team")                        # the primary passer per team
    out: dict[str, tuple[float, float, float, float]] = {}
    for _, r in q.iterrows():
        out[_canon_team(r["team"])] = (round(float(r["epa"]), 4), round(float(r["succ"]), 4),
                                       round(float(r["fppg"]), 2), round(float(r["rush_fppg"]), 2))
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
        # Data-driven single source of truth (NOT get_current_season()-1, a calendar
        # guess that reads 2024 in the Jan/Feb window while the line reads 2025). Every
        # Teams metric below — QB value, scheme, run-block, and the sack_rate the
        # pass-pro grade bells on — shares this one season.
        stats_season = latest_season_with_data()

    if pbp is None:
        import nfl_data_py as nfl
        pbp = nfl.import_pbp_data([stats_season], downcast=True)   # full load, then slice (never columns=)
    if ngs_passing is None:
        from backend.integrations.nfl_data import fetch_ngs_data
        ngs_passing = fetch_ngs_data("passing", stats_season)

    pass_rates = compute_pass_rates(pbp)
    qb_metrics = compute_qb_metrics(ngs_passing)            # cpoe/air (display only)
    qb_value = compute_qb_value(pbp)                        # EPA/success/fantasy-PPG (drives the tier)
    stuff_rates = compute_run_block_stuff_rate(pbp)          # slice 2
    personnel = compute_base_personnel(pbp)                  # slice 2
    red_zone = compute_red_zone_philosophy(pbp)              # slice 2

    rows = (await db.execute(select(TeamSystem))).scalars().all()
    latest = max((r.season_year for r in rows), default=None)
    rows = [r for r in rows if r.season_year == latest]

    # Gather the league's values for the RELATIVE widened-bell rank per metric. The QB
    # sub-composite bells over NON-rookie QBs only (rookies are a separate "rookie" tier).
    all_sack = [float(r.sack_rate) for r in rows if r.sack_rate is not None]
    all_stuff = [stuff_rates[_canon_team(r.team_abbr)] for r in rows
                 if _canon_team(r.team_abbr) in stuff_rates]
    _nonrook_qb = [_canon_team(r.team_abbr) for r in rows
                   if _canon_team(r.team_abbr) in qb_value and not r.rookie_qb_flag]
    all_epa = [qb_value[t][0] for t in _nonrook_qb]
    all_succ = [qb_value[t][1] for t in _nonrook_qb]
    all_fppg = [qb_value[t][2] for t in _nonrook_qb]
    all_rush = [qb_value[t][3] for t in _nonrook_qb]        # rushing-production pool (de-confound)

    scheme_n = passpro_n = qbtier_n = runblock_n = personnel_n = rz_n = 0
    missing_pr: list[str] = []
    missing_cpoe: list[str] = []
    missing_runblock: list[str] = []
    composite: dict = {}  # team_abbr → composite pct (for the system-grade bell)
    for r in rows:
        team = _canon_team(r.team_abbr)

        # 1. SCHEME + real pass rate (absolute label — not on the A–F bell)
        pr = pass_rates.get(team)
        if pr is None:
            missing_pr.append(r.team_abbr)
        else:
            r.oc_run_pass_split_tendency = Decimal(str(pr))
            r.oc_scheme = scheme_from_pass_rate(pr)
            scheme_n += 1

        # 2. PASS PROTECTION — widened-bell on real sack_rate (lower = better)
        pass_pct = bell_rank(float(r.sack_rate) if r.sack_rate is not None else None,
                             all_sack, lower_is_better=True)
        if r.sack_rate is None:
            logger.warning("team_metrics: %s missing sack_rate — pass_protection_grade left as-is", r.team_abbr)
        else:
            r.pass_protection_grade = grade_from_pct(pass_pct)
            passpro_n += 1

        # 3. QB TIER — a real EPA-weighted sub-composite (EPA + success + fantasy PPG
        #    incl. RUSHING), NOT cpoe-only (the accuracy fix: cpoe graded Mahomes/Jackson
        #    weak). cpoe/air are still stored for DISPLAY. The blended value feeds the bell.
        m = qb_metrics.get(team)
        if m is not None:
            cpoe, air = m
            r.qb_cpoe = Decimal(str(cpoe))
            r.qb_air_yards_per_attempt = Decimal(str(air))
        v = qb_value.get(team)
        qb_pct = None
        if v is None:
            missing_cpoe.append(r.team_abbr)
            if r.rookie_qb_flag:
                r.qb_tier = "rookie"
        else:
            epa, succ, fppg, rush_fppg = v
            qb_pct = qb_value_pct(
                bell_rank(epa, all_epa, lower_is_better=False),
                bell_rank(fppg, all_fppg, lower_is_better=False),
                bell_rank(succ, all_succ, lower_is_better=False),
                rush_pct=bell_rank(rush_fppg, all_rush, lower_is_better=False),
            )
            r.qb_tier = tier_from_pct(qb_pct, is_rookie=bool(r.rookie_qb_flag))
            qbtier_n += 1

        # 4. RUN BLOCKING — widened-bell on real stuff_rate (lower = better)
        sr = stuff_rates.get(team)
        run_pct = None
        if sr is None:
            missing_runblock.append(r.team_abbr)
        else:
            r.run_block_stuff_rate = Decimal(str(sr))
            run_pct = bell_rank(sr, all_stuff, lower_is_better=True)
            r.run_blocking_grade = grade_from_pct(run_pct)
            runblock_n += 1

        # 5. PERSONNEL — real base grouping from actual usage
        p = personnel.get(team)
        if p is not None:
            r.personnel_tendency = p
            personnel_n += 1

        # 6. RED ZONE — real weapon from RZ run/pass + receiver distribution
        rz = red_zone.get(team)
        if rz is not None:
            r.red_zone_philosophy = rz
            rz_n += 1

        # SYSTEM composite (real component percentiles — not an LLM letter).
        composite[r.team_abbr] = system_composite_pct(pass_pct, run_pct, qb_pct)

    # 7. SYSTEM/OVERALL GRADE — bell the composite across the 32 (distributes A–F).
    all_comp = [c for c in composite.values() if c is not None]
    system_n = 0
    for r in rows:
        c = composite.get(r.team_abbr)
        sys_pct = bell_rank(c, all_comp, lower_is_better=False)
        if sys_pct is not None:
            r.system_grade = grade_from_pct(sys_pct)
            system_n += 1

    await db.commit()
    if missing_pr:
        logger.warning("team_metrics: %d team(s) missing real pass_rate — scheme left as-is: %s", len(missing_pr), missing_pr)
    if missing_cpoe:
        logger.warning("team_metrics: %d team(s) missing QB-value stats — qb_tier left as-is (rookies excepted): %s", len(missing_cpoe), missing_cpoe)
    if missing_runblock:
        logger.warning("team_metrics: %d team(s) missing real stuff_rate — run_blocking_grade left as-is: %s", len(missing_runblock), missing_runblock)
    logger.info(
        "team_metrics (season %s → %d teams): scheme=%d pass_pro=%d qb_tier=%d "
        "run_block=%d system=%d personnel=%d red_zone=%d written (widened bell)",
        stats_season, len(rows), scheme_n, passpro_n, qbtier_n, runblock_n, system_n, personnel_n, rz_n,
    )
    return {
        "teams": len(rows), "scheme": scheme_n, "pass_pro": passpro_n,
        "qb_tier": qbtier_n, "run_block": runblock_n, "system": system_n,
        "personnel": personnel_n, "red_zone": rz_n, "missing_pass_rate": missing_pr,
        "missing_cpoe": missing_cpoe, "missing_runblock": missing_runblock,
    }
