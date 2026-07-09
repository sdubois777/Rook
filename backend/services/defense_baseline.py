"""
Defense (DST) preseason historical prior (K/DEF streaming arc — DEF prior slice).

Team defenses are structurally absent from the preseason PRIOR the value-engine
blend reads: the profiler is player-skill-only, so 0/32 DEF units had a
PlayerProfile → no ``clean_season_baseline`` → the blend got nothing and
early-season DST value was 0/garbage. But a DEF resolves to a real, team-keyed
player row with a canonical id ("Denver Broncos" → id), ``clean_season_baseline``
is keyed by player_id (a write path exists), and the per-week team-defense scorer
exists (kdef_scoring.score_dst_line / score_weekly_kdef). So — EXACTLY like the
kicker prior (#241) — the gap is a historical-total compute-and-write step, not a
structural one. This module mirrors kicker_baseline.py, TEAM-keyed instead of
player-keyed, reusing the shared recency-weight convention.

⚠️ HONEST SCOPE — CRUDE HISTORICAL PRIOR, NOT A PROJECTION. Team-defense scoring is
only WEAKLY predictive year-to-year (turnovers are noisy; personnel / scheme /
schedule turn over). A recency-weighted historical team-DST total is ADEQUATE to
stop early-season 0/garbage and is directionally sensible (good D > bad D), but it
is NOT an accurate DST projection — the talent/scheme-adjusted predictive model is
the DEFERRED L and is explicitly out of scope here. The in-season streaming/matchup
intelligence already lives in the DST tilt (apply_dst_matchup, #223) and rides ON
TOP of this prior — this module does not touch it.

DEF has no ROOKIE concept (team units don't debut), so a defense with no history in
the window (id-resolution miss) falls back to a league-average default and is
loud-warned — never a silent discard.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
from sqlalchemy import select

from backend.models.player import Player, PlayerProfile
from backend.utils.seasons import get_analysis_seasons, get_analysis_year
# Shared, position-agnostic convention (defined once in the kicker slice): the
# season-total games basis + the recency-weighted total. Reused verbatim so K and
# DEF priors share ONE tunable weighting scheme.
from backend.services.kicker_baseline import GAMES_BASIS, weighted_baseline_total

logger = logging.getLogger(__name__)

# --- tunables ----------------------------------------------------------------
# Completed seasons feeding the DEF baseline (same count as the kicker slice; the
# recency weights themselves live in kicker_baseline as the shared convention).
DEF_BASELINE_SEASONS = 3
# League-average team-defense PPG — the fallback SEASON TOTAL (× GAMES_BASIS) for a
# defense with no resolvable history. ~6.5 sits between a bad D (~5, the anchor
# replacement) and an elite D (~9-10). Only ever used on an id-resolution miss
# (every team has history), and loud-warned when it is.
DEF_DEFAULT_PPG = 6.5


def _default_total() -> float:
    return round(DEF_DEFAULT_PPG * GAMES_BASIS, 1)


# ---------------------------------------------------------------------------
# pure compute (fixture-injectable — no fetch, no DB)
# ---------------------------------------------------------------------------
def compute_defense_season_ppg(
    scored_by_season: dict[int, pd.DataFrame],
) -> dict[str, dict[int, float]]:
    """{canonical_player_id: {season: season_ppg}} for TEAM DEFENSES from per-season
    SCORED weekly K/DST frames (kdef_scoring.weekly_kdef_value_frame output).
    season_ppg = total DST fantasy points that season / games played. Non-DEF rows
    (kickers) are ignored. PURE — same shape as the kicker compute, DEF-filtered."""
    out: dict[str, dict[int, float]] = {}
    for season, frame in scored_by_season.items():
        if frame is None or getattr(frame, "empty", True):
            continue
        if "position" not in frame.columns:
            continue
        df = frame[frame["position"] == "DEF"]
        for pid, grp in df.groupby("canonical_player_id"):
            if pid is None:
                continue
            games = len(grp)
            if games == 0:
                continue
            ppg = float(grp["fantasy_points_ppr"].sum()) / games
            out.setdefault(str(pid), {})[int(season)] = round(ppg, 3)
    return out


# ---------------------------------------------------------------------------
# async write step (fetches cached weekly DST scoring; upserts PlayerProfile rows)
# ---------------------------------------------------------------------------
async def write_defense_baselines(
    db,
    *,
    scored_by_season: Optional[dict[int, pd.DataFrame]] = None,
    seasons: Optional[list[int]] = None,
) -> dict:
    """Compute + WRITE a preseason ``clean_season_baseline.ppr_points`` for every DEF
    (team-unit) player row. ``scored_by_season`` may be injected (tests) to avoid the
    fetch; otherwise the last ``DEF_BASELINE_SEASONS`` completed seasons of scored
    weekly K/DST frames are loaded. Returns a summary dict. Idempotent (upsert)."""
    seasons = seasons or get_analysis_seasons(DEF_BASELINE_SEASONS)
    seasons_desc = sorted(seasons, reverse=True)

    if scored_by_season is None:
        from backend.services.kdef_scoring import weekly_kdef_value_frame

        scored_by_season = {}
        for s in seasons:
            scored_by_season[s] = await weekly_kdef_value_frame(s, db=db)

    season_ppg_by_id = compute_defense_season_ppg(scored_by_season)

    from backend.agents.player_profiles import PLAYER_PROFILES_PROMPT_VERSION

    analysis_year = get_analysis_year()
    drows = (await db.execute(
        select(Player.id, Player.name).where(Player.position == "DEF")
    )).all()

    written = historical = default_used = 0
    for pid, name in drows:
        spid = str(pid)
        total = weighted_baseline_total(season_ppg_by_id.get(spid, {}), seasons_desc)
        if total is not None:
            source = "historical"
            confidence = "medium"
            historical += 1
        else:
            # Every team has history, so no-history means an id-resolution miss —
            # loud-warn (never a silent drop), then fall back to league average.
            total = _default_total()
            source = "no_history_default"
            confidence = "low"
            default_used += 1
            logger.warning(
                "defense baseline: DEF %s (%s) has no DST scoring in %s — using "
                "league-average default %.1f (id-resolution miss?)",
                name, spid, seasons_desc, total,
            )

        prof = (await db.execute(
            select(PlayerProfile).where(PlayerProfile.player_id == pid)
        )).scalar_one_or_none()
        if prof is None:
            prof = PlayerProfile(player_id=pid, season_year=analysis_year)
            db.add(prof)

        baseline = dict(prof.clean_season_baseline or {})
        baseline["ppr_points"] = total
        baseline["note"] = (
            f"Defense preseason prior ({source}) — recency-weighted historical DST "
            f"scoring (crude historical, not a projection)"
        )
        baseline["prompt_version"] = PLAYER_PROFILES_PROMPT_VERSION
        prof.clean_season_baseline = baseline
        prof.is_rookie = False  # team units are never rookies
        prof.profile_source = "defense_history" if source == "historical" else "defense_default"
        prof.confidence = confidence
        prof.anomalous_seasons_excluded = prof.anomalous_seasons_excluded or []
        written += 1

    await db.commit()
    logger.info(
        "defense baseline: wrote %d DEF profile(s) from seasons %s "
        "(%d historical, %d default)",
        written, seasons_desc, historical, default_used,
    )
    return {
        "written": written, "historical": historical,
        "default_used": default_used, "seasons": seasons_desc,
    }
