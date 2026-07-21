"""Value-snapshot service — capture / evaluate / outcome-writeback for value_snapshots.

Capture is EXPLICIT (a command, never pipeline-wired), APPEND-ONLY (ON CONFLICT DO NOTHING —
an existing snapshot row is never rewritten), and IDEMPOTENT/RESUMABLE (a re-run inserts only
missing rows). It captures the EFFECTIVE per-format value the board actually showed: PPR from
the players table (whose ai_bid_ceiling is populated — PFV's PPR row has NULL there), Half/
Standard from player_format_values, cross-format signals (value_gap/pay_up/nominate) on the
PPR basis (matching format_display.py).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.models.player import Player
from backend.models.player_format_values import PlayerFormatValues
from backend.models.value_snapshot import ValueSnapshot
from backend.agents.valuation_agent import VALUATION_AGENT_VERSION
from backend.agents.player_profiles import PLAYER_PROFILES_PROMPT_VERSION
from backend.integrations.nfl_data import get_seasonal_stats

logger = logging.getLogger(__name__)

_FORMATS = ("ppr", "half_ppr", "standard")
_SKILL = ("QB", "RB", "WR", "TE")
_RP = {"ppr": 1.0, "half_ppr": 0.5, "standard": 0.0}  # points per reception by format


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


def _gsis(p: Player) -> Optional[str]:
    return p.gsis_id or ((p.yahoo_player_id or "").replace("nfl_", "") or None)


async def compute_snapshot_rows(session: AsyncSession, season: int, label: str,
                                git_sha: Optional[str]) -> list[dict]:
    """Build one row-dict per (valued skill player × format) with the EFFECTIVE board value.
    Pure — no writes. Shared by the dry-run report and the real capture."""
    players = (await session.execute(
        select(Player).options(selectinload(Player.profile))
        .where(Player.position.in_(list(_SKILL)), Player.ai_bid_ceiling.isnot(None))
    )).scalars().all()
    pids = [p.id for p in players]
    pfv_rows = (await session.execute(
        select(PlayerFormatValues).where(PlayerFormatValues.player_id.in_(pids))
    )).scalars().all() if pids else []
    pfv = {(str(r.player_id), r.scoring_format): r for r in pfv_rows}

    rows: list[dict] = []
    for p in players:
        gsis = _gsis(p)
        csb = (p.profile.clean_season_baseline if (p.profile and p.profile.clean_season_baseline) else {}) or {}
        proj_ppr_season = csb.get("projected_ppr_season")
        for fmt in _FORMATS:
            row = pfv.get((str(p.id), fmt))
            if fmt == "ppr":
                # PPR-authoritative fields live on the players table (ai_bid_ceiling populated).
                baseline = _f(p.baseline_value); ai = int(p.ai_bid_ceiling) if p.ai_bid_ceiling is not None else None
                rec = _f(p.recommended_bid_ceiling); ceil = _f(p.ceiling_value)
                floor = _f(p.floor_value); risk = _f(p.risk_adjusted_value)
                tier = p.tier; assess = p.value_assessment
                proj = _f(proj_ppr_season)
                repl = _f(row.replacement_ppr) if row else None
                adp = _f(p.adp_fantasypros); mkt_fp = _f(p.market_value_fantasypros)
                pfv_missing = False
            elif row is not None:
                baseline = _f(row.baseline_value); ai = int(row.ai_bid_ceiling) if row.ai_bid_ceiling is not None else None
                rec = _f(row.recommended_bid_ceiling); ceil = _f(row.ceiling_value)
                floor = _f(row.floor_value); risk = _f(row.risk_adjusted_value)
                tier = row.tier; assess = row.value_assessment
                proj = _f(row.projected_points); repl = _f(row.replacement_ppr)
                adp = _f(row.adp_fantasypros); mkt_fp = _f(row.auction_value)
                pfv_missing = False
            else:
                # PFV row absent for a non-PPR format → board falls back to PPR (players) values.
                baseline = _f(p.baseline_value); ai = int(p.ai_bid_ceiling) if p.ai_bid_ceiling is not None else None
                rec = _f(p.recommended_bid_ceiling); ceil = _f(p.ceiling_value)
                floor = _f(p.floor_value); risk = _f(p.risk_adjusted_value)
                tier = p.tier; assess = p.value_assessment
                proj = _f(proj_ppr_season); repl = None
                adp = _f(p.adp_fantasypros); mkt_fp = _f(p.market_value_fantasypros)
                pfv_missing = True

            par = round(proj / repl, 3) if (proj and repl and repl > 0) else None
            rows.append({
                "season_year": season, "scoring_format": fmt, "snapshot_label": label,
                "player_id": p.id, "gsis_id": gsis, "sportradar_id": p.sportradar_id,
                "sleeper_id": p.sleeper_id, "player_name": p.name, "position": p.position,
                "projected_ppr": proj, "replacement_ppr": repl, "par_ratio": par, "tier": tier,
                "baseline_value": baseline, "recommended_bid_ceiling": rec, "ceiling_value": ceil,
                "floor_value": floor, "risk_adjusted_value": risk, "ai_bid_ceiling": ai,
                # cross-format signals stay on the PPR basis (matches format_display.py)
                "value_gap": _f(p.value_gap), "value_gap_signal": p.value_gap_signal,
                "value_assessment": assess, "pay_up_flag": bool(p.pay_up_flag),
                "nomination_target_flag": bool(p.nomination_target_flag),
                "market_value_fantasypros": mkt_fp, "adp_fantasypros": adp,
                "market_value_league": _f(p.market_value_league),
                "market_source": "fantasypros (adp+auction)",
                "market_fetched_at": p.market_value_updated_at,
                "valuation_agent_version": VALUATION_AGENT_VERSION,
                "profiles_prompt_version": PLAYER_PROFILES_PROMPT_VERSION,
                "git_sha": git_sha,
                # last_pipeline_run is unpopulated; players.updated_at is the value-write proxy
                "pipeline_run_at": p.updated_at,
                "_pfv_missing": pfv_missing,  # internal, stripped before insert
            })
    return rows


async def capture_snapshot(session: AsyncSession, season: int, label: str,
                           git_sha: Optional[str] = None, dry_run: bool = False) -> dict:
    """Capture (or dry-run) the snapshot. Append-only via ON CONFLICT DO NOTHING; existing
    rows are never rewritten. Returns a loud summary."""
    rows = await compute_snapshot_rows(session, season, label, git_sha)
    by_fmt: dict[str, int] = {}
    gsis_resolved = sum(1 for r in rows if r["gsis_id"])
    name_fallback = sum(1 for r in rows if not r["gsis_id"])
    pfv_missing = sum(1 for r in rows if r["_pfv_missing"])
    ppr_ai_null = sum(1 for r in rows if r["scoring_format"] == "ppr" and r["ai_bid_ceiling"] is None)
    for r in rows:
        by_fmt[r["scoring_format"]] = by_fmt.get(r["scoring_format"], 0) + 1

    inserted = skipped = 0
    if not dry_run:
        for r in rows:
            payload = {k: v for k, v in r.items() if not k.startswith("_")}
            # Targetless DO NOTHING so BOTH immutability guards fire: the gsis unique constraint
            # AND the name+position partial index (gsis-less players — NULLs don't collide on the
            # gsis constraint). Existing rows are never rewritten; a re-run inserts only what's missing.
            stmt = pg_insert(ValueSnapshot).values(**payload).on_conflict_do_nothing()
            res = await session.execute(stmt)
            if res.rowcount and res.rowcount > 0:
                inserted += 1
            else:
                skipped += 1
        await session.commit()

    return {
        "dry_run": dry_run, "season": season, "label": label,
        "rows_total": len(rows), "by_format": by_fmt,
        "gsis_resolved": gsis_resolved, "name_fallback": name_fallback,
        "pfv_missing_nonppr": pfv_missing, "ppr_ai_bid_ceiling_null": ppr_ai_null,
        "inserted": inserted, "already_present_skipped": skipped,
        "sample": rows[:3],
    }


async def write_outcomes(session: AsyncSession, season: int, dry_run: bool = False) -> dict:
    """Write back actual season points per format for all snapshot rows of `season` whose
    actual_points IS NULL. Matches on gsis_id → get_seasonal_stats; scores per format via the
    reception breakdown (now available for all seasons). Idempotent (only NULL rows)."""
    from sqlalchemy import update
    df = get_seasonal_stats(season)
    if df is None or df.empty:
        return {"season": season, "status": "no actuals available", "updated": 0}
    has_rec = "receptions" in df.columns
    actual = {}
    for _, r in df.iterrows():
        gid = str(r["player_id"])
        ppr = float(r["fantasy_points_ppr"] or 0)
        rec = float(r["receptions"] or 0) if has_rec else 0.0
        actual[gid] = {"ppr": ppr, "rec": rec, "games": int(r.get("games") or 0)}

    snaps = (await session.execute(
        select(ValueSnapshot).where(
            ValueSnapshot.season_year == season, ValueSnapshot.actual_points.is_(None))
    )).scalars().all()
    updated = 0; unmatched = 0; nonppr_uncomputable = 0
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for s in snaps:
        a = actual.get(s.gsis_id) if s.gsis_id else None
        if a is None:
            unmatched += 1
            continue
        fmt = s.scoring_format
        if fmt != "ppr" and not has_rec:
            nonppr_uncomputable += 1
            continue
        pts = a["ppr"] - (1.0 - _RP[fmt]) * a["rec"]
        if not dry_run:
            await session.execute(
                update(ValueSnapshot).where(ValueSnapshot.id == s.id)
                .values(actual_points=round(pts, 1), actual_games=a["games"], outcome_written_at=now))
        updated += 1
    if not dry_run:
        await session.commit()
    return {"season": season, "candidates_null": len(snaps), "updated": updated,
            "unmatched_gsis": unmatched, "nonppr_uncomputable": nonppr_uncomputable, "dry_run": dry_run}


async def evaluate_snapshot(session: AsyncSession, season: int, label: str,
                            scoring_format: str = "ppr") -> dict:
    """The reader that justifies the writer. Once outcomes land, tests whether our disagreements
    with the market predicted outperformance: regress our value ~ market and actual ~ market,
    then correlate the two residuals. Returns 'no outcomes yet' cleanly until write_outcomes runs.
    """
    rows = (await session.execute(
        select(ValueSnapshot).where(
            ValueSnapshot.season_year == season,
            ValueSnapshot.snapshot_label == label,
            ValueSnapshot.scoring_format == scoring_format,
        )
    )).scalars().all()
    if not rows:
        return {"status": "no snapshot rows", "season": season, "label": label, "format": scoring_format}

    scored = [r for r in rows
              if r.actual_points is not None and r.ai_bid_ceiling is not None
              and r.market_value_fantasypros is not None and float(r.market_value_fantasypros) > 0]
    if len(scored) < 10:
        return {"status": "no outcomes yet — cannot evaluate",
                "season": season, "label": label, "format": scoring_format,
                "snapshot_rows": len(rows), "rows_with_outcomes": len(scored),
                "note": "run write_outcomes(season) after the season completes"}

    import numpy as np
    our = np.array([float(r.ai_bid_ceiling) for r in scored])
    mkt = np.array([float(r.market_value_fantasypros) for r in scored])
    act = np.array([float(r.actual_points) for r in scored])
    sv_slope, sv_int = np.polyfit(mkt, our, 1)
    resid_value = our - (sv_slope * mkt + sv_int)          # our genuine disagreement ($)
    ap_slope, ap_int = np.polyfit(mkt, act, 1)
    resid_points = act - (ap_slope * mkt + ap_int)         # market's actual miss (points)
    r = float(np.corrcoef(resid_value, resid_points)[0, 1])
    return {"status": "evaluated", "season": season, "label": label, "format": scoring_format,
            "n": len(scored),
            "corr_residual_value_vs_outperformance": round(r, 3),
            "interpretation": ("positive => our disagreements predicted outperformance vs market"
                               if r > 0 else "<=0 => our disagreements did NOT predict outperformance")}
