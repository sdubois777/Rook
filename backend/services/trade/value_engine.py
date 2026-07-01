"""
In-season value engine — the differentiator (docs/trade_agent_design.md §3).

Every other trade tool leans on preseason projections → name bias + stale value.
This engine RE-DERIVES forward value from actual production + **usage
trajectory**, using the preseason projection only as a weak prior that the
in-season data overrides. The headline signal is the usage TREND (last-2 weeks
vs prior-3 weeks on snap% + target share), not the points level.

Inputs are the per-week rows from the #149 data layer
(``backend.integrations.nfl_weekly``): one row per (player, week) with at least
``week``, ``snap_pct``, ``target_share``, ``fantasy_points_ppr`` (plus
``targets`` / ``carries`` for the opportunity-vs-production gap). The engine is
pure/synchronous and fixture-injectable — it never fetches.

Name-bias guard (explicit, testable): value is computed only from usage /
production / the (down-weighted) prior. The player's NAME is never read in the
value path. When the role has decayed (usage falling) the preseason prior's
weight is *further* discounted so reputation cannot prop up a fading player —
``name_bias_guard_applied`` records when that fired.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

import pandas as pd

# --- positional anchors (replacement / elite) -------------------------------
# Anchors scale forward_ppg → 0-100 as points above positional REPLACEMENT,
# normalised to the position's ELITE ceiling. Anchors are DERIVED from the
# league's own player pool (see derive_anchors); the tuples below are the
# documented FALLBACK used only when a position is too sparse to derive.
_PPG_ANCHORS: dict[str, tuple[float, float]] = {
    "RB": (8.0, 24.0),
    "WR": (8.0, 23.0),
    "TE": (5.0, 17.0),
    "QB": (14.0, 28.0),
}
_DEFAULT_ANCHOR = (6.0, 20.0)

# Soft floor (0-100 scale): replacement maps HERE, not to 0, so producing
# below-replacement players keep a small, rank-preserving value instead of
# collapsing to an indistinguishable 0. Below replacement spreads 0..FLOOR by ppg;
# at/above replacement spreads FLOOR..100. Tunable.
_REPLACEMENT_FLOOR = 10.0

# League shape that defines REPLACEMENT via starter demand. Replacement = the
# waiver floor: in a `LEAGUE_TEAMS`-team league each team starts
# STARTERS_PER_POS[pos] (+ a share of FLEX_COUNT), so the best player just below
# that league-wide starter cutoff is replacement-level (QB replacement is high —
# everyone starts one; RB/WR replacement is deeper, reflecting real depth).
# NOTE: this replacement-level / positional-demand computation IS the positional-
# scarcity primitive the acceptability model will later consume — keep it shared.
LEAGUE_TEAMS = 12
STARTERS_PER_POS: dict[str, int] = {"QB": 1, "RB": 2, "WR": 3, "TE": 1}
FLEX_COUNT = 1
# v1 FLEX assumption: the flex spot is filled by RB/WR (split evenly); TE is
# rarely flexed. Tunable here so real leagues / the acceptability model reuse it.
FLEX_SPLIT: dict[str, float] = {"RB": 0.5, "WR": 0.5, "TE": 0.0, "QB": 0.0}
_ELITE_PCT = 0.95          # elite = 95th percentile of the position pool
_REPL_BAND = 5             # replacement = mean of this many waiver-tier players below the cutoff
_MIN_POOL_MARGIN = _REPL_BAND  # need cutoff + a full band to derive, else fall back

# Usage-trend threshold: composite (snap%+target share)/2 change, last-2 vs
# prior-3. 0.045 ≈ a ~5–9 point swing in snaps or target share.
_TREND_THRESHOLD = 0.045

# Opportunity-vs-production gap thresholds (PPR per game).
_GAP_BUY = -4.0   # producing well BELOW volume → buy (production catches up)
_GAP_SELL = 4.0   # producing well ABOVE volume → sell (regresses)

# Recency weights, most-recent week first (up to 3 weeks).
_RECENCY_WEIGHTS = (0.5, 0.3, 0.2)

# In-season LEVEL calibration: the level blends recency-weighted recent form with
# a season-to-date baseline, so a strong player's value stays anchored to his
# full body of work instead of collapsing onto a 3-week sample (the Chase-at-25
# bug). Tunable in ONE place — Stephen calibrates against real output.
#   1.0 → recency-only (the old, over-reactive behavior); 0.0 → season-only.
_RECENT_VS_SEASON_WEIGHT = 0.5

# Expected PPR points per opportunity (rough, for the volume-vs-production gap).
_PTS_PER_TARGET = 1.4
_PTS_PER_CARRY = 0.55

# Games needed before the in-season sample fully overrides the preseason prior.
_FULL_INSEASON_GAMES = 5.0
# Extra discount on the prior's weight when the role has decayed (name-bias guard).
_GUARD_PRIOR_DISCOUNT = 0.3

# --- value-trajectory wiring (docs/trade_value_trajectory_design.md) ---------
# The usage TRAJECTORY (§3.1) and the opportunity-vs-production GAP (§3.2)
# MULTIPLY forward_ppg (before anchor-scaling) so the founding differentiator
# actually moves value instead of only flipping a display flag. Both factors are
# centered at 1.0 (no signal → untouched, the #158 safety), SYMMETRIC (rising
# lifts / falling discounts), BOUNDED by a hard cap, and CONFIDENCE-SCALED (thin /
# cross-team trends can't whipsaw value — the Lamar safety). CONSERVATIVE defaults
# (decision 7a): the level still dominates; tune UP against real output. Each
# coefficient is independently settable to 0 to isolate one signal in calibration.
_TRAJECTORY_COEFFICIENT = 0.5    # usage-composite delta → factor strength
_TRAJECTORY_CAP = 0.12           # max |trajectory factor − 1|
_OPP_GAP_WEIGHT = 0.012          # PPR opportunity-gap → factor strength
_OPP_GAP_CAP = 0.10              # max |opp-gap factor − 1|
# The opp-gap is only meaningful where volume is MEASURED (targets + carries). QBs
# (passing volume isn't captured here) and players with no volume data get NO
# opp-gap adjustment — without this, expected≈0 makes every QB a phantom
# "over-producer" and craters them.
_OPP_GAP_MIN_EXPECTED = 1.0
# Confidence scaling for both factors: FULL → full adjustment; LIMITED (partial
# same-team window) → dampened; INSUFFICIENT or a team change (cross-offense trend
# = wrong signal) → none.
_LIMITED_TREND_SCALE = 0.5
# The COMBINED factor (trajectory × opp-gap) is clamped so two same-direction
# signals can't over-move a value — especially over-crater on the downside (§4).
_COMBINED_FACTOR_BOUNDS = (0.80, 1.20)

# --- ragged-history contract (irregular player histories) -------------------
# A real trend needs a 2-game recent window AND a non-empty prior window, so the
# minimum for FULL confidence is 4 played games (2 recent + ≥2 prior; at 5+ the
# prior window fills to the full 3). Below 2 games no trend can be formed at all.
_MIN_TREND_GAMES = 2     # < this → INSUFFICIENT (no trend, flags suppressed)
_FULL_TREND_GAMES = 4    # ≥ this (and same team) → FULL confidence
# Trade trends compare across the last-5 played-game window; a team change inside
# it means the two halves are different offenses → widen uncertainty.
_TREND_WINDOW = 5

# --- player availability / staleness (docs/trade_value_availability_design.md) ---
# The window operates over games PLAYED with no notion of WHEN — so an injured
# player keeps pre-injury value forever (the Kraft bug: out since wk9, read 70.9
# "sell-high" at wk14). The fix keys on weeks_stale = weeks since the last played
# game, BYES NOT COUNTED (a bye is neither a zero nor staleness — §2). Two SEPARATE
# mechanics ride that primitive:
#   (3a) staleness → CONFIDENCE: a stale player downgrades, and because the #170
#        trajectory/opp-gap factors are ALREADY confidence-scaled, the trend signal
#        dampens for free — no new trajectory machinery.
#   (3b) staleness → BASE-LEVEL DECAY: confidence-scaling only touches the trend
#        multiplier, NOT the base level (Kraft's 70.9 IS the stale base; prior_w=0
#        at ≥5 games). So decay the base forward_ppg toward the floor, applied like
#        the #170 factors (before anchor-scaling). This is the actual inflation fix.
# CONSERVATIVE curve (decision 8b): gentle for the first stale week, steep for a
# sustained absence. Free-weeks guard (decision 8c): weeks_stale ≤ 1 → NO decay and
# NO confidence hit, so a single missed game / a bye at the data edge is tolerated.
_STALENESS_FREE_WEEKS = 1          # ≤ this many stale weeks → untouched (bye / 1 game)
# Accelerating decay factor = 1 / (1 + RATE · effective^POWER), effective = stale − free.
# POWER is the TAIL-STEEPNESS knob: because effective=1 (a 2-week gap) is pinned at
# RATE·1=0.5 for ANY power, raising POWER steepens ONLY the long-absence tail
# (weeks_stale ≥ 3) while leaving the short-gap band byte-identical. POWER was 2.0 in
# #177 (too gentle: a 3-week-out player only ~halved → 0.33, and London read a
# still-startable 9.2 that masked The Lord's WR need). POWER 5.0 floors true
# season-enders so a multi-week absentee isn't startable:
#   stale 1 →1.00 · 2 →0.667 (UNCHANGED from #177) · 3 →0.059 (London → ~floor) ·
#   5 →0.002 (Kraft, floor) · 10 →~0 (Tyreek, floor). Short gaps (≤2) are NOT touched;
# only the tail. This is the minimum power that floors the 3-week London enough to
# deflate The Lord's inflated WR lineup AND unlock Fat Bastard at the UNCHANGED #174
# gate (recon-verified). Tunable in ONE place.
_STALENESS_DECAY_RATE = 0.5
_STALENESS_DECAY_POWER = 5.0
# Confidence downgrade by staleness past the free guard (effective stale weeks):
_STALENESS_LIMITED_WEEKS = 1       # eff ≥ 1 (stale ≥ 2) → at most LIMITED (trend dampened)
_STALENESS_INSUFFICIENT_WEEKS = 2  # eff ≥ 2 (stale ≥ 3) → INSUFFICIENT (no actionable flag)


class ValueTrend(str, Enum):
    RISING = "rising"
    FALLING = "falling"
    STABLE = "stable"


class Confidence(str, Enum):
    """Data-sufficiency of the in-season read, so a downstream agent can soften
    verdicts on thin/irregular history."""
    FULL = "full"            # enough same-team played games for a real trend
    LIMITED = "limited"      # partial window or a team change in the window
    INSUFFICIENT = "insufficient"  # < 2 played games — no trend at all


@dataclass
class InSeasonValue:
    canonical_player_id: str
    name: str
    position: str
    forward_value: float          # 0-100, position-relative
    value_trend: ValueTrend       # headline: usage trajectory
    buy_low: bool
    sell_high: bool
    why: str
    # --- transparency / testable sub-signals ---
    games_played: int
    usage_recent: float
    usage_prior: float
    usage_delta: float
    recency_ppg: float
    expected_ppg: float
    opportunity_gap: float        # recency_ppg − expected_ppg
    sustainable: bool
    forward_ppg: float
    schedule_modifier: float
    prior_projection: Optional[float]
    prior_weight: float
    name_bias_guard_applied: bool
    # --- ragged-history contract ---
    confidence: Confidence = Confidence.FULL
    confidence_reason: str = ""


# ---------------------------------------------------------------------------
# helpers (each independently testable)
# ---------------------------------------------------------------------------
def _rate(val) -> float:
    """Coerce a share/rate to a clean [0,1] float; NaN/None/garbage → 0.0.
    (``float('nan') or 0.0`` returns nan because nan is truthy — must guard.)"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    try:
        return max(0.0, min(1.0, float(val)))
    except (TypeError, ValueError):
        return 0.0


def _usage_composite(snap_pct: float, target_share: float) -> float:
    """Headline usage = mean of snap share and target share (both 0-1). For a QB
    target_share≈0, so the composite is snap-dominated — but the TREND (delta)
    still tracks snap movement, which is all we need for trajectory."""
    return (_rate(snap_pct) + _rate(target_share)) / 2.0


def _played_weeks(weeks: pd.DataFrame, current_week: int) -> pd.DataFrame:
    """Rows for weeks <= current_week, oldest→newest, with a usage composite.

    Each input row is one game the player PLAYED — byes/inactives produce no row
    in the #149 layer and are therefore skipped, never counted as 0-usage. We do
    NOT reindex to calendar weeks; the trend operates on games played.
    """
    df = weeks[weeks["week"] <= current_week].copy()
    df = df.sort_values("week")
    # Defensive numeric coercion so NaN/None can't poison the math downstream.
    for col in ("fantasy_points_ppr", "targets", "carries"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["_usage"] = df.apply(
        lambda r: _usage_composite(r.get("snap_pct", 0.0), r.get("target_share", 0.0)),
        axis=1,
    )
    return df


def _team_changed_in_window(df_played: pd.DataFrame, window: int = _TREND_WINDOW) -> bool:
    """True if the player was on more than one nfl_team across the trend window.
    If the per-week rows carry no team column, we can't detect it → False."""
    col = "nfl_team" if "nfl_team" in df_played.columns else (
        "recent_team" if "recent_team" in df_played.columns else None
    )
    if col is None:
        return False
    teams = [t for t in df_played.tail(window)[col].tolist()
             if t is not None and not (isinstance(t, float) and pd.isna(t))]
    return len(set(teams)) > 1


# ---------------------------------------------------------------------------
# availability / staleness (schedule-driven; the engine never fetches — byes are
# injected, exactly like weekly_usage)
# ---------------------------------------------------------------------------
def bye_weeks_from_schedule(schedule_df) -> dict[str, set[int]]:
    """{team: {bye_week, …}} from an nflverse schedule frame (REG only): a team
    absent from a week's games is on bye. PURE — pass the ALREADY-CACHED schedule
    (fetch_schedules / warehouse.schedule); the value engine never fetches."""
    out: dict[str, set[int]] = {}
    if schedule_df is None or getattr(schedule_df, "empty", True):
        return out
    df = schedule_df
    if "game_type" in df.columns:
        df = df[df["game_type"] == "REG"]
    teams = set(df["home_team"]).union(df["away_team"])
    weeks = set(int(w) for w in df["week"].tolist())
    played: dict[str, set[int]] = {t: set() for t in teams}
    for row in df.itertuples(index=False):
        played[getattr(row, "home_team")].add(int(getattr(row, "week")))
        played[getattr(row, "away_team")].add(int(getattr(row, "week")))
    for t in teams:
        out[t] = {w for w in weeks if w not in played[t]}
    return out


def weeks_stale(
    last_played_week: Optional[int],
    current_week: int,
    team_bye_weeks: Optional[set[int]] = None,
) -> int:
    """Weeks since the player last played — BYES NOT COUNTED (§2). A bye in the gap
    is neither an absence nor a zero, so it is subtracted: a player whose only
    missed week is his team's bye reads 0 stale, while a sustained injury reads the
    full run of missed games. Returns 0 when the player is current (or no history)."""
    if last_played_week is None or last_played_week >= current_week:
        return 0
    byes = team_bye_weeks or set()
    span = range(last_played_week + 1, current_week + 1)
    missed_byes = sum(1 for w in span if w in byes)
    return (current_week - last_played_week) - missed_byes


def _staleness_decay(stale: float) -> float:
    """Base-level decay multiplier (3b) — accelerating: gentle for the first stale
    week past the free guard, steep for a sustained absence. Centered at 1.0 (the
    #158 safety: a current player, stale ≤ free, is UNTOUCHED)."""
    effective = max(0.0, stale - _STALENESS_FREE_WEEKS)
    if effective <= 0:
        return 1.0
    return 1.0 / (1.0 + _STALENESS_DECAY_RATE * effective ** _STALENESS_DECAY_POWER)


def _assess_confidence(
    games: int, team_changed: bool, stale: float = 0.0,
) -> tuple[Confidence, str]:
    """Grade the in-season read by how much (and how clean) the history is — and
    how STALE it is. A long-absent player with plenty of (old) games is no longer
    FULL: staleness past the free guard downgrades confidence, which (because the
    #170 factors are confidence-scaled) automatically dampens his trend signal."""
    if games < _MIN_TREND_GAMES:
        return Confidence.INSUFFICIENT, f"only {games} played game(s) — no trend"
    effective_stale = stale - _STALENESS_FREE_WEEKS
    if effective_stale >= _STALENESS_INSUFFICIENT_WEEKS:
        return Confidence.INSUFFICIENT, (
            f"{int(stale)} weeks since last game — stale, no actionable read"
        )
    if team_changed:
        return Confidence.LIMITED, (
            f"team change within last-{_TREND_WINDOW} window — trajectory not "
            f"cleanly computable across a mid-window team change (cross-team "
            f"share denominator)"
        )
    if effective_stale >= _STALENESS_LIMITED_WEEKS:
        return Confidence.LIMITED, f"{int(stale)} weeks since last game — stale trend"
    if games < _FULL_TREND_GAMES:
        return Confidence.LIMITED, f"{games} played games — partial trend window"
    return Confidence.FULL, f"{games} played games"


def usage_trend(df_played: pd.DataFrame) -> tuple[float, float, float, ValueTrend]:
    """last-2 weeks vs prior-3 weeks on the usage composite → (recent, prior,
    delta, trend). The HEADLINE signal — trajectory, not points."""
    usage = df_played["_usage"].tolist()
    if len(usage) < 2:
        # not enough signal to call a trend
        recent = usage[-1] if usage else 0.0
        return recent, recent, 0.0, ValueTrend.STABLE
    recent = sum(usage[-2:]) / len(usage[-2:])
    prior_slice = usage[-5:-2] if len(usage) >= 3 else usage[:-2]
    prior = sum(prior_slice) / len(prior_slice) if prior_slice else recent
    delta = recent - prior
    if delta >= _TREND_THRESHOLD:
        trend = ValueTrend.RISING
    elif delta <= -_TREND_THRESHOLD:
        trend = ValueTrend.FALLING
    else:
        trend = ValueTrend.STABLE
    return round(recent, 4), round(prior, 4), round(delta, 4), trend


def recency_ppg(df_played: pd.DataFrame) -> float:
    """Recency-weighted PPR over the last up-to-3 weeks (most recent heaviest)."""
    pts = df_played["fantasy_points_ppr"].tolist()[-3:]
    if not pts:
        return 0.0
    pts = list(reversed(pts))  # most recent first
    weights = _RECENCY_WEIGHTS[: len(pts)]
    wsum = sum(weights)
    return round(sum(p * w for p, w in zip(pts, weights)) / wsum, 2)


def season_ppg(df_played: pd.DataFrame) -> float:
    """Season-to-date mean PPR per played game — the LEVEL anchor that keeps a
    strong player's value tied to his full body of work, not just recent weeks.
    (Byes/inactives have no row, so this is correctly per played game.)"""
    pts = df_played["fantasy_points_ppr"]
    if len(pts) == 0:
        return 0.0
    return round(float(pts.mean()), 2)


def inseason_level(df_played: pd.DataFrame) -> float:
    """The recency-blended in-season LEVEL — recency-weighted recent form anchored
    to the season-to-date baseline. This is the SAME quantity ``forward_ppg`` is
    built from (BEFORE the #170 trajectory/opp-gap factors and BEFORE anchor-
    scaling), so anchors derived from it share the value's basis. It depends ONLY
    on production (recency_ppg + season_ppg) — never on the anchors or
    forward_value — so there is no circular reference."""
    if len(df_played) == 0:
        return 0.0
    return round(
        _RECENT_VS_SEASON_WEIGHT * recency_ppg(df_played)
        + (1.0 - _RECENT_VS_SEASON_WEIGHT) * season_ppg(df_played),
        2,
    )


def expected_ppg_from_volume(df_played: pd.DataFrame) -> float:
    """Rough expected PPR from recent per-game opportunity (targets + carries).
    Used only relatively, for the opportunity-vs-production gap."""
    recent = df_played.tail(3)
    if recent.empty:
        return 0.0
    tpg = float(recent.get("targets", pd.Series([0])).mean() or 0.0)
    cpg = float(recent.get("carries", pd.Series([0])).mean() or 0.0)
    return round(tpg * _PTS_PER_TARGET + cpg * _PTS_PER_CARRY, 2)


def _scale_0_100(ppg: float, position: str, anchors: Optional[dict] = None) -> float:
    """Scale forward ppg → 0-100, position-relative. SOFT FLOOR (not a hard clamp
    to 0): a producing BELOW-replacement player gets a small positive,
    rank-preserving value instead of collapsing to 0 (which destroyed all ordering
    in the below-replacement band and made 45% of the pool an indistinguishable 0,
    corrupting contextual_value / acceptability). Mapping:
      • ppg ≥ replacement → linear replacement→FLOOR, elite→100 (startable band)
      • 0 < ppg < replacement → linear 0-ppg→0, replacement→FLOOR (sub-repl band)
      • ppg ≤ 0 / not producing → 0
    So a 7.9-ppg WR below an 8.7 anchor reads ~9, a 3-ppg scrub ~3.4 — distinct."""
    repl, elite = (anchors or _PPG_ANCHORS).get(position, _DEFAULT_ANCHOR)
    if elite <= repl:
        return 0.0
    if ppg <= 0:
        return 0.0
    if ppg >= repl:
        # Startable band: replacement → FLOOR, elite → 100 (compressed into FLOOR..100).
        val = _REPLACEMENT_FLOOR + (ppg - repl) / (elite - repl) * (100.0 - _REPLACEMENT_FLOOR)
    else:
        # Below-replacement band: spread 0..FLOOR by actual ppg (rank-preserving).
        val = (ppg / repl) * _REPLACEMENT_FLOOR
    return round(max(0.0, min(100.0, val)), 1)


def _value_confidence_scale(confidence: Confidence, team_changed: bool) -> float:
    """How much the trajectory / opp-gap factors may move value, by data trust.
    INSUFFICIENT or a team change (cross-offense trend = WRONG signal, not just
    thin) → no adjustment; a partial same-team window → dampened; FULL → full.
    This is the Lamar safety: a thin/returning-player trend can't whipsaw value."""
    if confidence is Confidence.INSUFFICIENT or team_changed:
        return 0.0
    if confidence is Confidence.LIMITED:
        return _LIMITED_TREND_SCALE
    return 1.0


def usage_trajectory_factor(usage_delta: float, scale: float) -> float:
    """§3.1 — symmetric, bounded, confidence-scaled multiplier from the usage
    composite TREND. Centered at 1.0; rising usage (delta>0) lifts (>1), falling
    discounts (<1). This is the half of the differentiator that was dead — buy-low
    now actually raises value, not just a flag."""
    raw = _TRAJECTORY_COEFFICIENT * usage_delta
    raw = max(-_TRAJECTORY_CAP, min(_TRAJECTORY_CAP, raw))
    return 1.0 + raw * scale


def opp_gap_factor(gap: float, expected_ppg: float, scale: float) -> float:
    """§3.2 — symmetric efficiency mean-reversion. gap = production − volume-implied;
    over-producing (gap>0, e.g. TD variance) discounts toward implied (sell-high),
    under-producing (gap<0) lifts (buy-low). No adjustment where volume isn't
    measured (QBs / missing targets+carries) — the general form of the old
    falling-and-over-producing `unsustainable_hot` valve (§4), now both directions."""
    if expected_ppg < _OPP_GAP_MIN_EXPECTED:
        return 1.0
    raw = -_OPP_GAP_WEIGHT * gap
    raw = max(-_OPP_GAP_CAP, min(_OPP_GAP_CAP, raw))
    return 1.0 + raw * scale


def _bound_combined(factor: float) -> float:
    """Clamp the trajectory × opp-gap PRODUCT so two same-direction signals can't
    over-move a value (the combined-downside-crater guard, §4)."""
    lo, hi = _COMBINED_FACTOR_BOUNDS
    return max(lo, min(hi, factor))


def _starter_demand(position: str, teams: int) -> float:
    """League-wide starters at a position = teams·starters + teams·flex·flex_share."""
    return (teams * STARTERS_PER_POS.get(position, 0)
            + teams * FLEX_COUNT * FLEX_SPLIT.get(position, 0.0))


def season_ppg_by_position(weekly_usage, roster_positions: dict[str, str]) -> dict[str, list[float]]:
    """{position: [season_ppg, …]} over the ROSTERED players (the league's player
    universe), from the per-week layer. Season ppg = mean PPR over played weeks."""
    out: dict[str, list[float]] = {}
    if weekly_usage is None or getattr(weekly_usage, "empty", True):
        return out
    for pid, grp in weekly_usage.groupby("canonical_player_id"):
        pos = roster_positions.get(pid)
        if pos not in ("QB", "RB", "WR", "TE"):
            continue
        out.setdefault(pos, []).append(float(grp["fantasy_points_ppr"].mean()))
    return out


def inseason_level_by_position(
    weekly_usage, roster_positions: dict[str, str], current_week: int,
) -> dict[str, list[float]]:
    """{position: [inseason_level, …]} over the ROSTERED players — the FORWARD-BASIS
    replacement for ``season_ppg_by_position`` as the anchor-derivation input. Each
    value is the recency-blended in-season LEVEL (the same basis ``forward_value``
    scales), so the replacement/elite anchors are measured in the same units as the
    players compared against them (the QB-anchor basis-mismatch fix). Pure
    production — no anchor / forward_value dependency, so derive_anchors stays
    non-circular."""
    out: dict[str, list[float]] = {}
    if weekly_usage is None or getattr(weekly_usage, "empty", True):
        return out
    for pid, grp in weekly_usage.groupby("canonical_player_id"):
        pos = roster_positions.get(pid)
        if pos not in ("QB", "RB", "WR", "TE"):
            continue
        out.setdefault(pos, []).append(inseason_level(_played_weeks(grp, current_week)))
    return out


def derive_anchors(
    season_ppg_by_pos: dict[str, list[float]],
    *,
    teams: int = LEAGUE_TEAMS,
) -> dict[str, tuple[float, float]]:
    """Derive {position: (replacement_ppg, elite_ppg)} from the pool's season-ppg
    distribution. Replacement = a small band around the starter-demand cutoff
    (the best player below the league-wide starter line); elite = the 95th
    percentile. Falls back to the hardcoded anchor for any position too sparse
    to derive or whose derived band is degenerate."""
    import numpy as np

    out: dict[str, tuple[float, float]] = {}
    for pos in ("QB", "RB", "WR", "TE"):
        vals = sorted((v for v in season_ppg_by_pos.get(pos, []) if v is not None),
                      reverse=True)
        cutoff = int(round(_starter_demand(pos, teams)))
        if len(vals) < cutoff + _MIN_POOL_MARGIN:
            out[pos] = _PPG_ANCHORS[pos]          # too sparse → documented fallback
            continue
        # Replacement = the WAIVER TIER: the best freely-available players, i.e.
        # the band of `_REPL_BAND` players ranked just BELOW the starter cutoff
        # (a band, not the single marginal starter, for stability). Elite = 95th
        # percentile of the position pool.
        lo, hi = cutoff, min(len(vals), cutoff + _REPL_BAND)
        repl = round(sum(vals[lo:hi]) / (hi - lo), 1)
        elite = round(float(np.quantile(vals, _ELITE_PCT)), 1)
        out[pos] = (repl, elite) if elite > repl else _PPG_ANCHORS[pos]
    return out


def replacement_ppg_by_position(values: dict[str, "InSeasonValue"]) -> dict[str, float]:
    """{position: replacement-level ppg} for the league's rostered pool — the SAME
    ``derive_anchors`` replacement the #172 anchors use, read in ``forward_ppg``
    units (what ``lineup_strength_ppg`` sums). Lets an unfillable starter slot be
    valued at the streamable-waiver floor instead of 0. Reuses ``derive_anchors``;
    introduces NO new constant (falls back to the documented ``_PPG_ANCHORS``
    replacement when a position is too sparse to derive)."""
    ppg_by_pos: dict[str, list[float]] = {}
    for v in values.values():
        if v.position in ("QB", "RB", "WR", "TE"):
            ppg_by_pos.setdefault(v.position, []).append(v.forward_ppg)
    anchors = derive_anchors(ppg_by_pos)
    return {pos: anchors[pos][0] for pos in ("QB", "RB", "WR", "TE")}


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------
def compute_player_value(
    *,
    canonical_player_id: str,
    name: str,
    position: str,
    weeks: pd.DataFrame,
    current_week: int,
    prior_projection_ppg: Optional[float] = None,
    schedule_modifier: float = 0.0,
    anchors: Optional[dict] = None,
    stale_weeks: float = 0.0,
) -> InSeasonValue:
    """Derive one player's in-season value from their per-week rows.

    ``prior_projection_ppg`` is the preseason expectation expressed as PPR per
    game — it enters ONLY as a weak prior, down-weighted as the in-season sample
    grows and discounted further when the role decays (name-bias guard).

    ``stale_weeks`` is weeks since the last played game (byes excluded — see
    ``weeks_stale``). 0 for a current player → the value is UNCHANGED (the #158
    safety). A sustained absence both downgrades confidence (dampening the #170
    trend factor) and decays the base level toward the floor.
    """
    df = _played_weeks(weeks, current_week)
    games = int(len(df))

    if games == 0:
        # No in-season data: fall back to the prior alone (neutral if absent).
        ppg = float(prior_projection_ppg or 0.0)
        return InSeasonValue(
            canonical_player_id=canonical_player_id, name=name, position=position,
            forward_value=_scale_0_100(ppg + schedule_modifier, position, anchors),
            value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False,
            why="no in-season data yet; preseason prior only",
            games_played=0, usage_recent=0.0, usage_prior=0.0, usage_delta=0.0,
            recency_ppg=0.0, expected_ppg=0.0, opportunity_gap=0.0,
            sustainable=True, forward_ppg=round(ppg, 2),
            schedule_modifier=schedule_modifier,
            prior_projection=prior_projection_ppg,
            prior_weight=1.0 if prior_projection_ppg is not None else 0.0,
            name_bias_guard_applied=False,
            confidence=Confidence.INSUFFICIENT,
            confidence_reason="no in-season games played",
        )

    team_changed = _team_changed_in_window(df)
    confidence, confidence_reason = _assess_confidence(games, team_changed, stale_weeks)

    u_recent, u_prior, u_delta, trend = usage_trend(df)
    form = recency_ppg(df)
    expected = expected_ppg_from_volume(df)
    gap = round(form - expected, 2)

    # Sustainability flag: hot scoring NOT backed by usage (falling role + scoring
    # above volume). Kept for the sell-high flag + transparency; its old direct
    # value-regression is now SUBSUMED by the symmetric opp-gap factor below (§4),
    # so it is NOT applied here (would double-discount a falling over-producer).
    unsustainable_hot = trend == ValueTrend.FALLING and gap >= _GAP_SELL
    sustainable = not unsustainable_hot

    # LEVEL (calibration fix): anchor the recency-weighted recent form to the
    # season-to-date baseline so a strong player in a mild recent dip doesn't
    # collapse onto a 3-week sample. The TREND signal above is left short-window.
    # This is the SAME quantity the anchors are derived from (inseason_level), so
    # value and anchor share a basis (the QB-anchor basis-mismatch fix).
    in_season_ppg = inseason_level(df)

    # Preseason prior enters weakly; washes out as games accrue.
    in_w = min(1.0, games / _FULL_INSEASON_GAMES)
    prior_w = 1.0 - in_w
    guard = False
    if prior_projection_ppg is not None and prior_w > 0:
        # Name-bias guard (KEPT — distinct from the trajectory factor: it suppresses
        # the PRESEASON PRIOR's weight when reputation outstrips a decayed role,
        # whereas the trajectory factor nudges the post-blend ppg by usage
        # direction). When the role has decayed (falling) and the prior outstrips
        # current production, discount the prior so reputation can't prop value up.
        if trend == ValueTrend.FALLING and prior_projection_ppg > in_season_ppg:
            prior_w *= _GUARD_PRIOR_DISCOUNT
            guard = True
    else:
        prior_w = 0.0

    prior_component = prior_projection_ppg if prior_projection_ppg is not None else in_season_ppg
    total_w = in_w + prior_w
    forward_ppg = (in_w * in_season_ppg + prior_w * prior_component) / (total_w or 1.0)
    forward_ppg = round(forward_ppg, 2)

    # --- TRAJECTORY + OPP-GAP factors: the differentiator, finally wired into VALUE.
    # Multiply forward_ppg BEFORE anchor-scaling (role change matters more in
    # absolute terms for high-value players). Both centered at 1.0, confidence-
    # scaled, and the product is clamped so two same-direction signals can't
    # over-move the value.
    tscale = _value_confidence_scale(confidence, team_changed)
    combined_factor = _bound_combined(
        usage_trajectory_factor(u_delta, tscale) * opp_gap_factor(gap, expected, tscale)
    )
    forward_ppg = round(forward_ppg * combined_factor, 2)

    # --- STALENESS base-level decay (3b): the availability fix. Applied like the
    # #170 factors — per-player, BEFORE anchor-scaling — so a player who hasn't
    # played in weeks decays toward the floor instead of holding stale pre-injury
    # value. Unconditional on confidence (confidence handles the trend half); 1.0
    # for a current player, so the frozen calibration is untouched.
    forward_ppg = round(forward_ppg * _staleness_decay(stale_weeks), 2)

    forward_value = _scale_0_100(forward_ppg + schedule_modifier, position, anchors)

    if confidence is Confidence.INSUFFICIENT:
        # Too little history to claim anything — don't fabricate a trend/flag.
        trend = ValueTrend.STABLE
        buy_low = sell_high = False
        why = confidence_reason
    elif team_changed:
        # Team change isn't too-LITTLE signal, it's WRONG signal: the usage trend
        # diffs two different offenses (the per-week target-share denominator is a
        # different team's targets before vs after the move), so the share delta
        # is not a real trajectory. We decline to assert an actionable buy/sell we
        # can't honestly derive — but, unlike insufficient, confidence stays
        # `limited` and value_trend keeps its RAW direction as a transparency
        # sub-signal. Recovering a true cross-team trajectory is the documented v2
        # cross-team-share-normalization item.
        buy_low = sell_high = False
        why = confidence_reason
    else:
        buy_low, sell_high, why = _flags_and_why(
            position=position, trend=trend, u_recent=u_recent, u_prior=u_prior,
            u_delta=u_delta, form=form, expected=expected, gap=gap,
            unsustainable_hot=unsustainable_hot, guard=guard,
        )

    return InSeasonValue(
        canonical_player_id=canonical_player_id, name=name, position=position,
        forward_value=forward_value, value_trend=trend,
        buy_low=buy_low, sell_high=sell_high, why=why,
        games_played=games, usage_recent=u_recent, usage_prior=u_prior,
        usage_delta=u_delta, recency_ppg=form, expected_ppg=expected,
        opportunity_gap=gap, sustainable=sustainable, forward_ppg=forward_ppg,
        schedule_modifier=schedule_modifier, prior_projection=prior_projection_ppg,
        prior_weight=round(prior_w / (total_w or 1.0), 3),
        name_bias_guard_applied=guard,
        confidence=confidence, confidence_reason=confidence_reason,
    )


def _flags_and_why(
    *, position, trend, u_recent, u_prior, u_delta, form, expected, gap,
    unsustainable_hot, guard,
) -> tuple[bool, bool, str]:
    """Map the signals to buy_low / sell_high + a one-line, usage-grounded why."""
    buy_low = False
    sell_high = False
    reasons: list[str] = []

    if trend == ValueTrend.RISING:
        buy_low = True
        reasons.append(
            f"usage rising (composite {u_prior:.0%}→{u_recent:.0%} last 2 wks)"
        )
    elif trend == ValueTrend.FALLING:
        sell_high = True
        reasons.append(
            f"usage falling (composite {u_prior:.0%}→{u_recent:.0%} last 2 wks)"
        )

    if gap <= _GAP_BUY:
        buy_low = True
        reasons.append(
            f"producing below volume ({form:.0f} vs ~{expected:.0f} expected) — buy-low"
        )
    if unsustainable_hot:
        sell_high = True
        reasons.append(
            f"scoring above usage ({form:.0f} vs ~{expected:.0f}) on a fading role — regression risk"
        )

    # If both fire (rare crosscurrents), prefer the headline usage trend.
    if buy_low and sell_high:
        if trend == ValueTrend.FALLING:
            buy_low = False
        else:
            sell_high = False

    if guard:
        reasons.append("reputation down-weighted (role decayed)")
    if not reasons:
        reasons.append(
            f"usage stable (~{u_recent:.0%}), production in line with volume"
        )
    return buy_low, sell_high, "; ".join(reasons)


# ---------------------------------------------------------------------------
# league-level convenience
# ---------------------------------------------------------------------------
def evaluate_league(
    league_state,
    weekly_usage: pd.DataFrame,
    *,
    priors: Optional[dict[str, float]] = None,
    schedule_modifiers: Optional[dict[str, float]] = None,
    bye_weeks: Optional[dict[str, set[int]]] = None,
) -> dict[str, InSeasonValue]:
    """Value every rostered player in a ``LeagueState`` against the per-week
    usage table (the #149 layer output). Pure — inject ``weekly_usage`` for tests.

    ``bye_weeks`` ({team: {bye_week, …}}, from the already-cached schedule via
    ``bye_weeks_from_schedule``) lets per-player staleness EXCLUDE byes. When
    omitted, staleness uses the raw played-week gap (a single bye is still absorbed
    by the free-weeks guard); pass it to correctly discount multi-week absences
    that straddle a bye.
    """
    priors = priors or {}
    schedule_modifiers = schedule_modifiers or {}
    bye_weeks = bye_weeks or {}
    by_player = {
        pid: grp for pid, grp in weekly_usage.groupby("canonical_player_id")
    } if not weekly_usage.empty else {}

    # Derive positional anchors ONCE from this league's rostered pool (the player
    # universe LeagueState provides), so replacement/elite reflect real depth
    # rather than guesses. Falls back to hardcoded per-position when too sparse.
    # BASIS: the recency-blended in-season LEVEL (inseason_level_by_position), the
    # SAME quantity forward_value scales — NOT raw season_ppg. Deriving the anchor
    # on the value's own basis fixes the QB mismatch (a QB whose forward ran 3-5 ppg
    # below his season ppg was measured against a season-derived replacement and
    # read below it despite being a clear starter).
    roster_positions = {
        rp.canonical_player_id: rp.position
        for team in league_state.teams for rp in team.roster
    }
    anchors = derive_anchors(
        inseason_level_by_position(weekly_usage, roster_positions, league_state.week)
    )

    out: dict[str, InSeasonValue] = {}
    for team in league_state.teams:
        for rp in team.roster:
            weeks = by_player.get(
                rp.canonical_player_id,
                pd.DataFrame(columns=["week", "snap_pct", "target_share",
                                      "fantasy_points_ppr", "targets", "carries"]),
            )
            stale = _stale_weeks_for(weeks, rp, league_state.week, bye_weeks)
            out[rp.canonical_player_id] = compute_player_value(
                canonical_player_id=rp.canonical_player_id,
                name=rp.name,
                position=rp.position,
                weeks=weeks,
                current_week=league_state.week,
                prior_projection_ppg=priors.get(rp.canonical_player_id),
                schedule_modifier=schedule_modifiers.get(rp.canonical_player_id, 0.0),
                anchors=anchors,
                stale_weeks=stale,
            )
    return out


def _stale_weeks_for(weeks, rp, current_week: int, bye_weeks: dict[str, set[int]]) -> float:
    """Per-player staleness for ``evaluate_league``: last played week from the
    #149 rows, team resolved from the most-recent row's ``nfl_team`` (falling back
    to ``RosterPlayer.nfl_team``), byes from the injected schedule map."""
    if weeks is None or getattr(weeks, "empty", True):
        return 0.0
    played = weeks[weeks["week"] <= current_week]
    if played.empty:
        return 0.0
    played = played.sort_values("week")
    last_played = int(played["week"].max())
    team = None
    if "nfl_team" in played.columns:
        recent_team = played["nfl_team"].dropna()
        team = recent_team.iloc[-1] if len(recent_team) else None
    team = team or getattr(rp, "nfl_team", None)
    return weeks_stale(last_played, current_week, bye_weeks.get(team, set()) if team else set())
