"""
Kicker preseason historical prior (K/DEF streaming arc — kicker prior slice).

Kickers are structurally absent from the preseason PRIOR the value-engine blend
reads: the offense profiler is skill-only (SKILL_POSITIONS={QB,RB,WR,TE}), so
0/43 kickers had a PlayerProfile → no ``clean_season_baseline`` → the blend got
nothing and early-season kicker value was 0/garbage. But K already resolves to
real player rows with canonical ids (nfl_weekly.weekly_kdef_usage),
``clean_season_baseline`` is keyed by player_id (a write path exists), and the
per-week scorer exists (kdef_scoring.score_weekly_kdef). The only missing piece
is a historical compute-and-write step — THIS module.

DESIGN (agreed scope — NOT a predictive model): kicker output is low-variance, so
the preseason baseline is a RECENCY-WEIGHTED mean of prior completed seasons'
per-game K scoring, expressed as a SEASON TOTAL (per-game rate × 17) to match the
``clean_season_baseline.ppr_points`` convention the blend loads (÷17 → PPG).

This is a DEDICATED step: it does NOT modify the offense-only profiling pass
(SKILL_POSITIONS stays {QB,RB,WR,TE}); it writes the SAME field (ppr_points) for
K player rows only. Rookie / no-history kickers get the position default
(_ROOKIE_DEFAULT_PPG["K"]), closing the rookie-kicker double-gap. Every kicker
without real history is loud-warned (veterans) or noted (rookies) — never a silent
discard. Pure/synchronous compute helpers are fixture-injectable; the async writer
fetches the cached weekly K scoring and upserts PlayerProfile rows.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
from sqlalchemy import select

from backend.models.player import Player, PlayerProfile
from backend.utils.seasons import get_analysis_seasons, get_analysis_year

logger = logging.getLogger(__name__)

# --- tunables (ONE place) ----------------------------------------------------
# How many completed seasons feed the baseline, and the recency weights
# (most-recent first). Kicker scoring is low-variance → a weighted mean suffices.
KICKER_BASELINE_SEASONS = 3
KICKER_RECENCY_WEIGHTS = (0.5, 0.3, 0.2)
# Season-total = weighted per-game rate × GAMES_BASIS. 17 matches the blend's
# ÷17 (trade_demo_source._load_priors), so the round-trip recovers the ppg.
GAMES_BASIS = 17


def _rookie_default_total() -> float:
    """League-average kicker SEASON TOTAL for rookie / no-history kickers, derived
    from the shared _ROOKIE_DEFAULT_PPG['K'] (the founder's one-place default)."""
    from backend.agents.player_profiles import _ROOKIE_DEFAULT_PPG

    return round(_ROOKIE_DEFAULT_PPG["K"] * GAMES_BASIS, 1)


# ---------------------------------------------------------------------------
# pure compute (fixture-injectable — no fetch, no DB)
# ---------------------------------------------------------------------------
def compute_kicker_season_ppg(
    scored_by_season: dict[int, pd.DataFrame],
) -> dict[str, dict[int, float]]:
    """{canonical_player_id: {season: season_ppg}} from per-season SCORED weekly
    K frames (kdef_scoring.weekly_kdef_value_frame output). season_ppg = total K
    fantasy points that season / games PLAYED (byes/inactives have no row, so this
    is correctly per played game). Non-K rows are ignored. PURE."""
    out: dict[str, dict[int, float]] = {}
    for season, frame in scored_by_season.items():
        if frame is None or getattr(frame, "empty", True):
            continue
        if "position" not in frame.columns:
            continue
        kf = frame[frame["position"] == "K"]
        for pid, grp in kf.groupby("canonical_player_id"):
            if pid is None:
                continue
            games = len(grp)
            if games == 0:
                continue
            ppg = float(grp["fantasy_points_ppr"].sum()) / games
            out.setdefault(str(pid), {})[int(season)] = round(ppg, 3)
    return out


def weighted_baseline_total(
    season_ppg: dict[int, float], seasons_desc: list[int],
) -> Optional[float]:
    """Recency-weighted SEASON TOTAL from a kicker's per-season ppg map.

    ``seasons_desc`` is most-recent-first; the recency weights are applied to the
    seasons the kicker actually has (renormalised over available seasons, so a
    kicker with only 1-2 seasons still gets a clean weighted mean). Returns None
    when there's no season data (→ caller falls back to the position default)."""
    pairs = [(s, season_ppg[s]) for s in seasons_desc if s in season_ppg]
    if not pairs:
        return None
    weights = KICKER_RECENCY_WEIGHTS[: len(pairs)]
    wsum = sum(weights)
    ppg = sum(p * w for (_, p), w in zip(pairs, weights)) / (wsum or 1.0)
    return round(ppg * GAMES_BASIS, 1)


# ---------------------------------------------------------------------------
# async write step (fetches cached weekly K scoring; upserts PlayerProfile rows)
# ---------------------------------------------------------------------------
async def write_kicker_baselines(
    db,
    *,
    scored_by_season: Optional[dict[int, pd.DataFrame]] = None,
    seasons: Optional[list[int]] = None,
) -> dict:
    """Compute + WRITE a preseason ``clean_season_baseline.ppr_points`` for every K
    player row. ``scored_by_season`` may be injected (tests) to avoid the fetch;
    otherwise the last ``KICKER_BASELINE_SEASONS`` completed seasons of scored
    weekly K frames are loaded. Returns a summary dict. Idempotent (upsert)."""
    seasons = seasons or get_analysis_seasons(KICKER_BASELINE_SEASONS)
    seasons_desc = sorted(seasons, reverse=True)

    if scored_by_season is None:
        from backend.services.kdef_scoring import weekly_kdef_value_frame

        scored_by_season = {}
        for s in seasons:
            scored_by_season[s] = await weekly_kdef_value_frame(s, db=db)

    season_ppg_by_id = compute_kicker_season_ppg(scored_by_season)

    from backend.agents.player_profiles import PLAYER_PROFILES_PROMPT_VERSION

    analysis_year = get_analysis_year()
    krows = (await db.execute(
        select(Player.id, Player.name, Player.is_rookie).where(Player.position == "K")
    )).all()

    written = historical = rookie_default = vet_default = 0
    for pid, name, is_rookie in krows:
        spid = str(pid)
        total = weighted_baseline_total(season_ppg_by_id.get(spid, {}), seasons_desc)
        if total is not None:
            source = "historical"
            confidence = "medium"
            historical += 1
        elif is_rookie:
            total = _rookie_default_total()
            source = "rookie_default"
            confidence = "low"
            rookie_default += 1
        else:
            # A veteran kicker with NO K scoring in the window is unusual (retired /
            # id-resolution miss) — loud-warn, never a silent drop, then default.
            total = _rookie_default_total()
            source = "veteran_no_history_default"
            confidence = "low"
            vet_default += 1
            logger.warning(
                "kicker baseline: veteran K %s (%s) has no K scoring in %s — "
                "using league-average default %.1f", name, spid, seasons_desc, total,
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
            f"Kicker preseason prior ({source}) — recency-weighted historical K scoring"
        )
        baseline["prompt_version"] = PLAYER_PROFILES_PROMPT_VERSION
        prof.clean_season_baseline = baseline
        prof.is_rookie = bool(is_rookie)
        prof.profile_source = "kicker_history" if source == "historical" else "kicker_default"
        prof.confidence = confidence
        prof.anomalous_seasons_excluded = prof.anomalous_seasons_excluded or []
        written += 1

    await db.commit()
    logger.info(
        "kicker baseline: wrote %d K profile(s) from seasons %s "
        "(%d historical, %d rookie-default, %d veteran-default)",
        written, seasons_desc, historical, rookie_default, vet_default,
    )
    return {
        "written": written, "historical": historical,
        "rookie_default": rookie_default, "vet_default": vet_default,
        "seasons": seasons_desc,
    }
