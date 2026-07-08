"""
DST matchup-weekly tilt (K/DEF streaming arc, slice 4).

Replaces flat season DST forward_ppg with a GENTLE matchup tilt — DST ONLY — driven
by the EMPIRICALLY-VALIDATED signal from the 2025 holdout backtest: the opponent's
SACKS-ALLOWED per game as-of-W-1 (Spearman +0.20, the strongest single signal), with
opponent giveaways/g and points-scored/g as LIGHT secondaries.

Deliberate NON-choices, grounded in the holdout (do NOT "fix" these):
  * Vegas implied total is NOT used — it was weak AND mis-signed (+0.12 wrong
    direction) and took a negative blend weight; it fights the outcome.
  * The stored TeamSystem offensive grades are NOT used — season-static, qualitative,
    look-ahead-biased (not holdout-safe). The signal is derived fresh from PBP as-of-
    W-1 (the slice-1 mirror), reusing the raw weekly K/DST frame.
  * ONE signal, not a blend — a 4-signal OLS blend LOST to sacks-allowed alone
    out-of-sample; sacks dominates, giveaways/points are light tilts only.
  * KICKER gets NO matchup tilt — own-team implied total was -0.16 (no honest
    signal). K keeps its slice-2 SEASON forward_ppg. A near-flat K number IS the
    honest output; do not build a K matchup projection.

Holdout-safe: the opponent-offense signal uses weeks 1..W-1 ONLY. The tilt is CAPPED
so the matchup adjustment stays within ~+/-2.5 ppw of the season baseline — weekly DST
is mostly noise (one defensive TD = 6 pts of variance), and the real matchup-
explainable spread is only ~1-3 pts. An aggressive swing would fabricate signal that
the data does not support.
"""
from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# --- tunable tilt constants (ONE place) --------------------------------------
# Raw-unit coefficients, oriented so a sack/giveaway-prone opponent -> HIGHER DST.
# Orientation from the in-sample fit: sacks >> points ~= giveaways.
DST_TILT_SACKS_COEF = 1.3        # per (opp sacks-allowed/g - league mean)
DST_TILT_GIVEAWAYS_COEF = 0.7    # per (opp giveaways/g - league mean)
DST_TILT_POINTS_COEF = 0.06      # per (opp points-scored/g - league mean), applied NEGATIVELY
DST_TILT_CAP = 2.5               # |tilt| <= this many ppw — gentle by design

_DEF = "DEF"


def opponent_by_team(schedule, week: int) -> dict[str, str]:
    """{team_abbr: opponent_abbr} for a single week from the schedule."""
    out: dict[str, str] = {}
    for r in schedule.itertuples():
        if int(r.week) != int(week):
            continue
        out[r.home_team] = r.away_team
        out[r.away_team] = r.home_team
    return out


def build_offense_signal(kdef_raw, schedule, upto_week: int) -> dict[str, dict]:
    """{offense_team: {sacks_allowed_pg, giveaways_pg, points_pg, games}} over weeks
    1..upto_week ONLY (holdout-safe). Derived from each team's OPPONENT's DST line —
    the slice-1 mirror, so no extra PBP pass: a team's sacks-allowed == the sacks its
    opponent's defense recorded; giveaways == opp INT + fumble recoveries;
    points-scored == opp DST points_allowed (= this team's own final score)."""
    if kdef_raw is None or getattr(kdef_raw, "empty", True):
        return {}
    dst = kdef_raw[kdef_raw["position"] == _DEF]
    opp: dict[tuple[str, int], str] = {}
    for r in schedule.itertuples():
        opp[(r.home_team, int(r.week))] = r.away_team
        opp[(r.away_team, int(r.week))] = r.home_team

    acc: dict[str, dict] = defaultdict(lambda: {"sacks": [], "gv": [], "pts": []})
    for r in dst.itertuples():
        wk = int(r.week)
        if wk > upto_week:
            continue
        offense = opp.get((r.nfl_team, wk))          # the offense this DST faced
        if offense is None:
            continue
        acc[offense]["sacks"].append(float(r.sacks))
        acc[offense]["gv"].append(float(r.interceptions) + float(r.fumble_recoveries))
        acc[offense]["pts"].append(float(r.points_allowed))

    out: dict[str, dict] = {}
    for team, a in acc.items():
        n = len(a["sacks"])
        if n == 0:
            continue
        out[team] = {
            "sacks_allowed_pg": sum(a["sacks"]) / n,
            "giveaways_pg": sum(a["gv"]) / n,
            "points_pg": sum(a["pts"]) / n,
            "games": n,
        }
    return out


def league_means(signal: dict[str, dict]) -> dict[str, float]:
    teams = list(signal.values())
    n = len(teams) or 1
    return {
        "sacks": sum(t["sacks_allowed_pg"] for t in teams) / n,
        "gv": sum(t["giveaways_pg"] for t in teams) / n,
        "pts": sum(t["points_pg"] for t in teams) / n,
    }


def dst_tilt(opp_sig: dict, means: dict[str, float]) -> float:
    """Gentle, capped tilt from one opponent's as-of-W-1 offense signal. Centered on
    the league mean, so an average-matchup opponent yields 0 (baseline unchanged)."""
    raw = (
        DST_TILT_SACKS_COEF * (opp_sig["sacks_allowed_pg"] - means["sacks"])
        + DST_TILT_GIVEAWAYS_COEF * (opp_sig["giveaways_pg"] - means["gv"])
        - DST_TILT_POINTS_COEF * (opp_sig["points_pg"] - means["pts"])
    )
    return round(max(-DST_TILT_CAP, min(DST_TILT_CAP, raw)), 2)


def apply_dst_tilt(values, dst_team_by_id, offense_signal, opp_map, week) -> dict:
    """Return a new values dict with each DST's forward_ppg = season baseline + gentle
    matchup tilt. DST ONLY — offense and kicker values are copied through untouched.
    Loud-warns any DST with no opponent (bye) or no opponent signal — kept at flat
    baseline, never dropped."""
    if not offense_signal:
        logger.warning("dst matchup: no opponent-offense signal — all DST kept flat (season baseline)")
        return values
    means = league_means(offense_signal)
    out = dict(values)
    applied = flat = 0
    for pid, team in dst_team_by_id.items():
        v = values.get(pid)
        if v is None or v.position != _DEF:
            continue
        opp = opp_map.get(team)
        sig = offense_signal.get(opp) if opp else None
        if sig is None:
            flat += 1
            logger.warning("dst matchup wk%s: no opponent/signal for %s DST — kept at season baseline", week, team)
            continue
        out[pid] = dataclasses.replace(v, forward_ppg=round(v.forward_ppg + dst_tilt(sig, means), 2))
        applied += 1
    logger.info("dst matchup wk%s: tilted %d DST, %d kept flat (bye/no-signal)", week, applied, flat)
    return out


def apply_dst_matchup(values, state, *, season: int, week: int) -> dict:
    """Post-evaluate DST-only matchup override for the demo seeds (the injection
    seam). Loads the cached raw weekly K/DST frame + schedule, builds the as-of-
    (week-1) opponent-offense signal, and tilts each DST's forward_ppg. Offense +
    kicker untouched. Safe no-op (loud-warn) if the weekly frame is unavailable."""
    import nfl_data_py as nfl

    from backend.integrations.nfl_weekly import compute_weekly_kdef

    raw = compute_weekly_kdef(season)
    if raw is None or getattr(raw, "empty", True):
        logger.warning("dst matchup: weekly K/DST frame unavailable for %s — DST kept flat", season)
        return values
    sch = nfl.import_schedules([season])
    sch = sch[sch["game_type"] == "REG"]
    signal = build_offense_signal(raw, sch, upto_week=week - 1)
    opp_map = opponent_by_team(sch, week)
    dst_team_by_id = {
        rp.canonical_player_id: rp.nfl_team
        for t in state.teams for rp in t.roster if rp.position == _DEF
    }
    return apply_dst_tilt(values, dst_team_by_id, signal, opp_map, week)
