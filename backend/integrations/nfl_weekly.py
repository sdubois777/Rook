"""
Per-week NFL data layer — snap %, target share, and fantasy points keyed by
(canonical_player_id, season, week).

This is the data foundation for the in-season trade value engine (usage
trajectory: is a player's target/snap/carry share rising or falling?). The
existing accessors in ``nfl_data.py`` (``compute_target_share`` /
``compute_snap_pct`` / Sleeper target share) all collapse to **season
averages** — useless for trajectory. This module produces one row per player
per week and never collapses to season.

Sources (verified during the slice's Step-0 recon):
  * **Production + target share** come from raw play-by-play. nflverse has NOT
    published 2025 weekly ``player_stats`` (``import_weekly_data([2025])`` 404s),
    so production is derived from PBP — mirroring ``compute_seasonal_stats_from_pbp``'s
    verified scoring formula (CMC 414.6 / Allen 378.6 / Nacua 377.0 for 2025),
    but grouped per (player, week). Summed back over weeks it reproduces those
    season totals — that is the apples-to-apples guarantee (see tests).
  * **Snap %** comes from the per-week ``snaps_{season}.parquet`` (offense_pct),
    NOT the season-aggregate ``compute_snap_pct``.

Player-id join (no pre-existing nflverse→Rook map existed — built here, and it
is load-bearing for the trade slices that follow): nflverse keys its feeds by
``gsis_id`` (PBP) and ``pfr_player_id`` (snaps); Rook's canonical key is the
``players`` UUID. ``nfl.import_ids()`` is the crosswalk
(gsis ↔ sleeper ↔ sportradar ↔ pfr). We bridge each nflverse id to a Rook player
in reliability order **sleeper_id → sportradar_id → gsis_id** (Rook's standard
ID-first priority; sleeper_id has the best Rook-side coverage, 3943/4219).

Week-agnostic by design: every function takes ``season`` and an optional
``weeks`` filter. There is no hardcoded current-week here — that pin is demo
scaffolding for a later trade slice and must never live in this layer.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

import nfl_data_py as nfl
import pandas as pd

from backend.integrations.nfl_data import (
    SKILL_POSITIONS,
    _cache_path,
    fetch_snap_counts,
)

logger = logging.getLogger(__name__)

# Scoring constants — identical to compute_seasonal_stats_from_pbp so weekly
# fantasy points, summed over a season, reproduce the verified season totals.
_PPR_PER_REC = 1.0
_PT_PER_REC_YARD = 0.1
_PT_PER_RUSH_YARD = 0.1
_PT_PER_PASS_YARD = 0.04
_PT_PER_REC_TD = 6.0
_PT_PER_RUSH_TD = 6.0
_PT_PER_PASS_TD = 4.0
_PT_PER_INT = -2.0
_PT_PER_FUMBLE = -2.0

# PBP flag/yard columns coerced to numeric before aggregation.
_FLAG_COLS = (
    "pass_attempt",
    "complete_pass",
    "touchdown",
    "pass_touchdown",
    "interception",
    "fumble_lost",
)
_YARD_COLS = ("receiving_yards", "rushing_yards", "passing_yards")


# ---------------------------------------------------------------------------
# id normalisation + crosswalk
# ---------------------------------------------------------------------------
def _norm_id(val) -> Optional[str]:
    """Normalise an id to a bare string, dropping float artifacts ('7564.0').

    import_ids() sometimes carries sleeper/espn ids as floats; Rook stores them
    as plain strings. Never JSON-parse — just strip a trailing '.0'.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return None
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def load_id_bridge(use_cache: bool = True) -> pd.DataFrame:
    """nflverse player-id crosswalk (gsis ↔ pfr ↔ sleeper ↔ sportradar).

    Cached to parquet — it is season-agnostic and changes only as nflverse adds
    players. Returns the id columns plus name/position for debugging.
    """
    path = _cache_path("nflverse_id_bridge")
    if use_cache and path.exists():
        return pd.read_parquet(path)

    ids = nfl.import_ids()
    keep = [c for c in ("gsis_id", "pfr_id", "sleeper_id", "sportradar_id",
                        "position", "name", "merge_name") if c in ids.columns]
    bridge = ids[keep].copy()
    for col in ("gsis_id", "pfr_id", "sleeper_id", "sportradar_id"):
        if col in bridge.columns:
            bridge[col] = bridge[col].map(_norm_id)
    try:
        bridge.to_parquet(path, index=False)
    except Exception as exc:  # caching is best-effort
        logger.warning("Could not cache id bridge: %s", exc)
    return bridge


def _source_to_canonical_map(
    bridge: pd.DataFrame,
    id_type: str,
    player_maps: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Build {nflverse source id -> Rook canonical uuid} for one id family.

    ``id_type`` is 'gsis' (PBP) or 'pfr' (snaps). For each crosswalk row we take
    its (sleeper, sportradar, gsis) ids and resolve to a Rook player in priority
    order sleeper → sportradar → gsis.
    """
    src_col = "gsis_id" if id_type == "gsis" else "pfr_id"
    sleeper_map = player_maps.get("sleeper", {})
    sr_map = player_maps.get("sportradar", {})
    gsis_map = player_maps.get("gsis", {})

    out: dict[str, str] = {}
    for row in bridge.itertuples(index=False):
        src = _norm_id(getattr(row, src_col, None))
        if not src:
            continue
        sleeper = _norm_id(getattr(row, "sleeper_id", None))
        sportradar = _norm_id(getattr(row, "sportradar_id", None))
        gsis = _norm_id(getattr(row, "gsis_id", None))

        uuid = None
        if sleeper and sleeper in sleeper_map:
            uuid = sleeper_map[sleeper]
        elif sportradar and sportradar in sr_map:
            uuid = sr_map[sportradar]
        elif gsis and gsis in gsis_map:
            uuid = gsis_map[gsis]
        if uuid is not None and src not in out:
            out[src] = uuid
    return out


def attach_canonical_ids(
    df: pd.DataFrame,
    id_col: str,
    id_type: str,
    *,
    bridge: pd.DataFrame,
    player_maps: dict[str, dict[str, str]],
) -> pd.DataFrame:
    """Add a ``canonical_player_id`` column resolved via the crosswalk.

    Unresolved rows get None (kept, not dropped — callers decide). Pure: the
    bridge and the Rook id maps are injected, so this is unit-testable with no
    DB or network.
    """
    if df.empty:
        df = df.copy()
        df["canonical_player_id"] = None
        return df
    mapping = _source_to_canonical_map(bridge, id_type, player_maps)
    df = df.copy()
    df["canonical_player_id"] = df[id_col].map(lambda v: mapping.get(_norm_id(v)))
    return df


# ---------------------------------------------------------------------------
# per-week production + target share (from PBP)
# ---------------------------------------------------------------------------
def compute_weekly_pbp(
    season: int,
    pbp: Optional[pd.DataFrame] = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Per-(player, week) production + target share derived from raw PBP.

    Mirrors ``compute_seasonal_stats_from_pbp`` scoring exactly, but groups by
    week instead of season. Target share is per-week
    ``player_targets / team_targets`` — the same definition the season path uses
    before it averages over weeks, so weekly-vs-season is apples-to-apples.

    Pass ``pbp`` to inject a frame (tests); otherwise raw PBP is fetched live.
    Result is cached to ``weekly_pbp_{season}.parquet``.
    """
    path = _cache_path(f"weekly_pbp_{season}")
    if use_cache and pbp is None and path.exists():
        return pd.read_parquet(path)

    if pbp is None:
        # NEVER pass columns= to import_pbp_data — triggers KeyError 'game_id'
        # on 2025 (nflverse schema change). Load full, slice after.
        pbp = nfl.import_pbp_data([season])

    if pbp is None or pbp.empty or "season_type" not in pbp.columns:
        logger.warning("PBP unavailable/empty for %d", season)
        return pd.DataFrame()

    pbp = pbp[pbp["season_type"] == "REG"].copy()
    for col in _FLAG_COLS:
        pbp[col] = pd.to_numeric(pbp.get(col), errors="coerce").fillna(0).astype(int)
    for col in _YARD_COLS:
        pbp[col] = pd.to_numeric(pbp.get(col), errors="coerce").fillna(0.0)

    # --- Receiving (keyed by receiver) ---
    rec_src = pbp[pbp["receiver_player_id"].notna()].copy()
    rec_src["rec_yards"] = rec_src["receiving_yards"] * rec_src["complete_pass"]
    rec_src["rec_td"] = rec_src["complete_pass"] * rec_src["touchdown"]
    rec = (
        rec_src.groupby(["receiver_player_id", "week", "posteam"], dropna=False)
        .agg(
            player_name=("receiver_player_name", "first"),
            targets=("pass_attempt", "sum"),
            receptions=("complete_pass", "sum"),
            receiving_yards=("rec_yards", "sum"),
            receiving_tds=("rec_td", "sum"),
        )
        .reset_index()
        .rename(columns={"receiver_player_id": "player_id"})
    )
    # Team targets per (week, team) — denominator for per-week target share.
    team_tgt = (
        rec.groupby(["week", "posteam"], dropna=False)["targets"].sum()
        .reset_index().rename(columns={"targets": "team_targets"})
    )
    rec = rec.merge(team_tgt, on=["week", "posteam"], how="left")
    rec["target_share"] = rec["targets"] / rec["team_targets"].replace(0, pd.NA)

    # --- Rushing (keyed by rusher) ---
    rush = (
        pbp[pbp["rusher_player_id"].notna()]
        .groupby(["rusher_player_id", "week", "posteam"], dropna=False)
        .agg(
            rush_name=("rusher_player_name", "first"),
            carries=("rush_attempt", "size") if "rush_attempt" in pbp.columns
            else ("rushing_yards", "size"),
            rushing_yards=("rushing_yards", "sum"),
            rushing_tds=("touchdown", "sum"),
        )
        .reset_index()
        .rename(columns={"rusher_player_id": "player_id"})
    )

    # --- Passing (keyed by passer) ---
    passing = (
        pbp[pbp["passer_player_id"].notna()]
        .groupby(["passer_player_id", "week", "posteam"], dropna=False)
        .agg(
            pass_name=("passer_player_name", "first"),
            passing_yards=("passing_yards", "sum"),
            passing_tds=("pass_touchdown", "sum"),
            interceptions=("interception", "sum"),
        )
        .reset_index()
        .rename(columns={"passer_player_id": "player_id"})
    )

    # --- Fumbles lost (keyed by fumbler) ---
    fum_src = pbp[(pbp["fumble_lost"] == 1) & (pbp["fumbled_1_player_id"].notna())]
    fum = (
        fum_src.groupby(["fumbled_1_player_id", "week"], dropna=False)
        .size().reset_index(name="fumbles_lost")
        .rename(columns={"fumbled_1_player_id": "player_id"})
    )

    # --- Unify on (player_id, week): an RB both rushes and receives ---
    out = rec.merge(rush, on=["player_id", "week", "posteam"], how="outer")
    out = out.merge(passing, on=["player_id", "week", "posteam"], how="outer")
    out = out.merge(fum, on=["player_id", "week"], how="outer")

    # Coalesce the name + team across roles.
    out["player_name"] = (
        out.get("player_name").combine_first(out.get("rush_name"))
        .combine_first(out.get("pass_name"))
    )
    out = out.drop(columns=[c for c in ("rush_name", "pass_name") if c in out.columns])
    out = out.rename(columns={"posteam": "recent_team"})

    count_cols = [
        "targets", "receptions", "receiving_yards", "receiving_tds",
        "carries", "rushing_yards", "rushing_tds",
        "passing_yards", "passing_tds", "interceptions", "fumbles_lost",
    ]
    for col in count_cols:
        if col not in out.columns:
            out[col] = 0
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
    out["team_targets"] = pd.to_numeric(out.get("team_targets"), errors="coerce").fillna(0)
    out["target_share"] = pd.to_numeric(out.get("target_share"), errors="coerce").fillna(0.0)

    out["fantasy_points_ppr"] = (
        out["receptions"] * _PPR_PER_REC
        + out["receiving_yards"] * _PT_PER_REC_YARD
        + out["receiving_tds"] * _PT_PER_REC_TD
        + out["rushing_yards"] * _PT_PER_RUSH_YARD
        + out["rushing_tds"] * _PT_PER_RUSH_TD
        + out["passing_yards"] * _PT_PER_PASS_YARD
        + out["passing_tds"] * _PT_PER_PASS_TD
        + out["interceptions"] * _PT_PER_INT
        + out["fumbles_lost"] * _PT_PER_FUMBLE
    ).round(2)
    # Standard = PPR minus the per-reception point.
    out["fantasy_points_std"] = (out["fantasy_points_ppr"] - out["receptions"]).round(2)

    out["season"] = season
    out["games"] = 1  # one row == one game-week
    out["carries"] = out["carries"].astype(int)
    out = out.sort_values(["player_id", "week"]).reset_index(drop=True)

    cols = [
        "player_id", "player_name", "recent_team", "season", "week", "games",
        "targets", "team_targets", "target_share",
        "receptions", "receiving_yards", "receiving_tds",
        "carries", "rushing_yards", "rushing_tds",
        "passing_yards", "passing_tds", "interceptions", "fumbles_lost",
        "fantasy_points_ppr", "fantasy_points_std",
    ]
    out = out[[c for c in cols if c in out.columns]]

    if use_cache:
        try:
            out.to_parquet(path, index=False)
        except Exception as exc:
            logger.warning("Could not cache weekly PBP for %d: %s", season, exc)
    return out


# ---------------------------------------------------------------------------
# per-week snap %
# ---------------------------------------------------------------------------
def compute_weekly_snaps(
    season: int,
    snaps: Optional[pd.DataFrame] = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Per-(player, week) offensive snap share, from the raw snaps feed.

    Regular season + skill positions only. ``snap_pct`` is nflverse's
    ``offense_pct`` (already a 0–1 fraction). One row per (pfr_player_id, week);
    NOT the season-aggregate ``compute_snap_pct``.
    """
    path = _cache_path(f"weekly_snap_pct_{season}")
    if use_cache and snaps is None and path.exists():
        return pd.read_parquet(path)

    if snaps is None:
        snaps = fetch_snap_counts(season)
    if snaps is None or snaps.empty:
        return pd.DataFrame()

    snaps = snaps.copy()
    if "game_type" in snaps.columns:
        snaps = snaps[snaps["game_type"] == "REG"]
    if "position" in snaps.columns:
        snaps = snaps[snaps["position"].isin(SKILL_POSITIONS)]

    out = snaps[[
        "pfr_player_id", "player", "position", "team", "season", "week",
        "offense_snaps", "offense_pct",
    ]].copy()
    out = out.rename(columns={"offense_pct": "snap_pct", "player": "snap_player_name"})
    out = out.drop_duplicates(["pfr_player_id", "week"]).reset_index(drop=True)

    if use_cache:
        try:
            out.to_parquet(path, index=False)
        except Exception as exc:
            logger.warning("Could not cache weekly snaps for %d: %s", season, exc)
    return out


# ---------------------------------------------------------------------------
# Rook player-id maps + the combined public accessor
# ---------------------------------------------------------------------------
async def load_player_maps(db) -> dict[str, dict[str, str]]:
    """Load {id_type -> {rook_id -> canonical uuid}} from the players table."""
    from sqlalchemy import select

    from backend.models.player import Player

    rows = (
        await db.execute(
            select(Player.id, Player.sleeper_id, Player.sportradar_id, Player.gsis_id)
        )
    ).all()
    sleeper: dict[str, str] = {}
    sportradar: dict[str, str] = {}
    gsis: dict[str, str] = {}
    for pid, sl, sr, gs in rows:
        uuid = str(pid)
        if (k := _norm_id(sl)):
            sleeper.setdefault(k, uuid)
        if (k := _norm_id(sr)):
            sportradar.setdefault(k, uuid)
        if (k := _norm_id(gs)):
            gsis.setdefault(k, uuid)
    return {"sleeper": sleeper, "sportradar": sportradar, "gsis": gsis}


def _filter_weeks(df: pd.DataFrame, weeks: Optional[Iterable[int]]) -> pd.DataFrame:
    if weeks is None or df.empty:
        return df
    wanted = set(int(w) for w in weeks)
    return df[df["week"].isin(wanted)].reset_index(drop=True)


def build_weekly_usage(
    season: int,
    player_maps: dict[str, dict[str, str]],
    *,
    bridge: Optional[pd.DataFrame] = None,
    pbp_weekly: Optional[pd.DataFrame] = None,
    snaps_weekly: Optional[pd.DataFrame] = None,
    weeks: Optional[Iterable[int]] = None,
) -> pd.DataFrame:
    """Combine per-week PBP production + snaps into one canonical-keyed table.

    Pure/synchronous: every dependency (id maps, crosswalk, the two per-week
    frames) can be injected, so this is unit-testable without DB or network.
    One row per (canonical_player_id, season, week).
    """
    if bridge is None:
        bridge = load_id_bridge()
    if pbp_weekly is None:
        pbp_weekly = compute_weekly_pbp(season)
    if snaps_weekly is None:
        snaps_weekly = compute_weekly_snaps(season)

    pbp_weekly = attach_canonical_ids(
        pbp_weekly, "player_id", "gsis", bridge=bridge, player_maps=player_maps
    )
    snaps_weekly = attach_canonical_ids(
        snaps_weekly, "pfr_player_id", "pfr", bridge=bridge, player_maps=player_maps
    )

    prod = pbp_weekly[pbp_weekly["canonical_player_id"].notna()].copy()

    snap_cols = ["canonical_player_id", "week", "snap_pct", "offense_snaps",
                 "position", "snap_team", "snap_player_name"]
    if snaps_weekly.empty or "canonical_player_id" not in snaps_weekly.columns:
        snap_slim = pd.DataFrame(columns=snap_cols)
    else:
        snap = snaps_weekly[snaps_weekly["canonical_player_id"].notna()].copy()
        snap = snap.rename(columns={"team": "snap_team"})
        snap_slim = snap[[c for c in snap_cols if c in snap.columns]].copy()

    merged = prod.merge(snap_slim, on=["canonical_player_id", "week"], how="outer")

    merged["season"] = season
    merged["player_name"] = merged.get("player_name").combine_first(
        merged.get("snap_player_name")
    )
    merged["nfl_team"] = merged.get("recent_team").combine_first(merged.get("snap_team"))

    # Numeric defaults for rows present in only one source.
    fill_zero = [
        "targets", "team_targets", "target_share", "receptions",
        "receiving_yards", "receiving_tds", "carries", "rushing_yards",
        "rushing_tds", "passing_yards", "passing_tds", "interceptions",
        "fumbles_lost", "fantasy_points_ppr", "fantasy_points_std",
        "snap_pct", "offense_snaps",
    ]
    for col in fill_zero:
        if col not in merged.columns:
            merged[col] = 0
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)

    merged = merged.rename(columns={"position": "position"})
    final_cols = [
        "canonical_player_id", "player_name", "position", "nfl_team",
        "season", "week", "snap_pct", "target_share", "targets", "team_targets",
        "receptions", "receiving_yards", "receiving_tds",
        "carries", "rushing_yards", "rushing_tds",
        "passing_yards", "passing_tds", "interceptions", "fumbles_lost",
        "fantasy_points_ppr", "fantasy_points_std",
    ]
    merged = merged[[c for c in final_cols if c in merged.columns]]
    merged = merged.sort_values(["canonical_player_id", "week"]).reset_index(drop=True)
    return _filter_weeks(merged, weeks)


async def weekly_player_usage(
    season: int,
    weeks: Optional[Iterable[int]] = None,
    db=None,
) -> pd.DataFrame:
    """Public accessor: per-(canonical_player_id, season, week) usage table.

    Resolves Rook ids from the DB, computes/loads the per-week PBP + snaps
    tables, and merges. ``weeks`` optionally restricts the output (e.g.
    ``range(1, 6)`` for weeks 1–5) — no week is hardcoded in this layer.
    """
    own_db = db is None
    if own_db:
        from backend.database import AsyncSessionLocal

        db = AsyncSessionLocal()
    try:
        player_maps = await load_player_maps(db)
    finally:
        if own_db:
            await db.close()

    return build_weekly_usage(season, player_maps, weeks=weeks)
