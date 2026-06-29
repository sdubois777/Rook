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

# --- v1 anchors / thresholds (documented, position-relative) ----------------
# Forward points-per-game (PPR) anchors per position: (replacement, elite) used
# to scale forward_ppg → 0-100. v1 reuses the draft side's VORP/tier intuition:
# value is points above positional replacement, normalised to the elite ceiling.
_PPG_ANCHORS: dict[str, tuple[float, float]] = {
    "RB": (8.0, 24.0),
    "WR": (8.0, 23.0),
    "TE": (5.0, 17.0),
    "QB": (14.0, 28.0),
}
_DEFAULT_ANCHOR = (6.0, 20.0)

# Usage-trend threshold: composite (snap%+target share)/2 change, last-2 vs
# prior-3. 0.045 ≈ a ~5–9 point swing in snaps or target share.
_TREND_THRESHOLD = 0.045

# Opportunity-vs-production gap thresholds (PPR per game).
_GAP_BUY = -4.0   # producing well BELOW volume → buy (production catches up)
_GAP_SELL = 4.0   # producing well ABOVE volume → sell (regresses)

# Recency weights, most-recent week first (up to 3 weeks).
_RECENCY_WEIGHTS = (0.5, 0.3, 0.2)

# Expected PPR points per opportunity (rough, for the volume-vs-production gap).
_PTS_PER_TARGET = 1.4
_PTS_PER_CARRY = 0.55

# Games needed before the in-season sample fully overrides the preseason prior.
_FULL_INSEASON_GAMES = 5.0
# Extra discount on the prior's weight when the role has decayed (name-bias guard).
_GUARD_PRIOR_DISCOUNT = 0.3


class ValueTrend(str, Enum):
    RISING = "rising"
    FALLING = "falling"
    STABLE = "stable"


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


# ---------------------------------------------------------------------------
# helpers (each independently testable)
# ---------------------------------------------------------------------------
def _usage_composite(snap_pct: float, target_share: float) -> float:
    """Headline usage = mean of snap share and target share (both 0-1). For a QB
    target_share≈0, so the composite is snap-dominated — but the TREND (delta)
    still tracks snap movement, which is all we need for trajectory."""
    return (float(snap_pct or 0.0) + float(target_share or 0.0)) / 2.0


def _played_weeks(weeks: pd.DataFrame, current_week: int) -> pd.DataFrame:
    """Rows for weeks <= current_week, oldest→newest, with a usage composite."""
    df = weeks[weeks["week"] <= current_week].copy()
    df = df.sort_values("week")
    df["_usage"] = df.apply(
        lambda r: _usage_composite(r.get("snap_pct", 0.0), r.get("target_share", 0.0)),
        axis=1,
    )
    return df


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


def expected_ppg_from_volume(df_played: pd.DataFrame) -> float:
    """Rough expected PPR from recent per-game opportunity (targets + carries).
    Used only relatively, for the opportunity-vs-production gap."""
    recent = df_played.tail(3)
    if recent.empty:
        return 0.0
    tpg = float(recent.get("targets", pd.Series([0])).mean() or 0.0)
    cpg = float(recent.get("carries", pd.Series([0])).mean() or 0.0)
    return round(tpg * _PTS_PER_TARGET + cpg * _PTS_PER_CARRY, 2)


def _scale_0_100(ppg: float, position: str) -> float:
    repl, elite = _PPG_ANCHORS.get(position, _DEFAULT_ANCHOR)
    if elite <= repl:
        return 0.0
    val = (ppg - repl) / (elite - repl) * 100.0
    return round(max(0.0, min(100.0, val)), 1)


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
) -> InSeasonValue:
    """Derive one player's in-season value from their per-week rows.

    ``prior_projection_ppg`` is the preseason expectation expressed as PPR per
    game — it enters ONLY as a weak prior, down-weighted as the in-season sample
    grows and discounted further when the role decays (name-bias guard).
    """
    df = _played_weeks(weeks, current_week)
    games = int(len(df))

    if games == 0:
        # No in-season data: fall back to the prior alone (neutral if absent).
        ppg = float(prior_projection_ppg or 0.0)
        return InSeasonValue(
            canonical_player_id=canonical_player_id, name=name, position=position,
            forward_value=_scale_0_100(ppg + schedule_modifier, position),
            value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False,
            why="no in-season data yet; preseason prior only",
            games_played=0, usage_recent=0.0, usage_prior=0.0, usage_delta=0.0,
            recency_ppg=0.0, expected_ppg=0.0, opportunity_gap=0.0,
            sustainable=True, forward_ppg=round(ppg, 2),
            schedule_modifier=schedule_modifier,
            prior_projection=prior_projection_ppg, prior_weight=1.0,
            name_bias_guard_applied=False,
        )

    u_recent, u_prior, u_delta, trend = usage_trend(df)
    form = recency_ppg(df)
    expected = expected_ppg_from_volume(df)
    gap = round(form - expected, 2)

    # Sustainability: hot scoring NOT backed by usage (falling role + scoring
    # above volume) is unsustainable → regress toward volume-implied output.
    unsustainable_hot = trend == ValueTrend.FALLING and gap >= _GAP_SELL
    sustainable = not unsustainable_hot
    in_season_ppg = form
    if unsustainable_hot:
        in_season_ppg = round(0.6 * form + 0.4 * expected, 2)

    # Preseason prior enters weakly; washes out as games accrue.
    in_w = min(1.0, games / _FULL_INSEASON_GAMES)
    prior_w = 1.0 - in_w
    guard = False
    if prior_projection_ppg is not None and prior_w > 0:
        # Name-bias guard: when the role has decayed (falling usage) and the
        # prior (reputation) outstrips current production, discount the prior so
        # reputation can't prop the value up.
        if trend == ValueTrend.FALLING and prior_projection_ppg > in_season_ppg:
            prior_w *= _GUARD_PRIOR_DISCOUNT
            guard = True
    else:
        prior_w = 0.0

    prior_component = prior_projection_ppg if prior_projection_ppg is not None else in_season_ppg
    total_w = in_w + prior_w
    forward_ppg = (in_w * in_season_ppg + prior_w * prior_component) / (total_w or 1.0)
    forward_ppg = round(forward_ppg, 2)
    forward_value = _scale_0_100(forward_ppg + schedule_modifier, position)

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
) -> dict[str, InSeasonValue]:
    """Value every rostered player in a ``LeagueState`` against the per-week
    usage table (the #149 layer output). Pure — inject ``weekly_usage`` for tests.
    """
    priors = priors or {}
    schedule_modifiers = schedule_modifiers or {}
    by_player = {
        pid: grp for pid, grp in weekly_usage.groupby("canonical_player_id")
    } if not weekly_usage.empty else {}

    out: dict[str, InSeasonValue] = {}
    for team in league_state.teams:
        for rp in team.roster:
            weeks = by_player.get(
                rp.canonical_player_id,
                pd.DataFrame(columns=["week", "snap_pct", "target_share",
                                      "fantasy_points_ppr", "targets", "carries"]),
            )
            out[rp.canonical_player_id] = compute_player_value(
                canonical_player_id=rp.canonical_player_id,
                name=rp.name,
                position=rp.position,
                weeks=weeks,
                current_week=league_state.week,
                prior_projection_ppg=priors.get(rp.canonical_player_id),
                schedule_modifier=schedule_modifiers.get(rp.canonical_player_id, 0.0),
            )
    return out
