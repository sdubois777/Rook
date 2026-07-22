#!/usr/bin/env python
"""Phase 1 — market-free FLAT PPR board baseline (READ-ONLY; makes no DB writes).

The honest starting point any reshape must beat. Every number in the report is produced
by THIS script (run it to reproduce):

    .venv/Scripts/python.exe scripts/reshape_phase1_baseline.py

Design (leak-free held-out):
  * Projection for eval season S = weighted average of PRIOR realized PPR (S-1,S-2,S-3 at
    0.5/0.3/0.2, renormalized) — uses ONLY seasons < S, so no eval-season leakage. This is a
    reproducible proxy for the pipeline's baseline; the CURVE-SHAPE question is invariant to
    the projector's identity (see the rank-invariance result below).
  * Flat market-free board = the engine's own PAR pool-share $ (ppr_to_system_value over the
    projection), i.e. compute_bid_ceiling with the FantasyPros/ANCHOR_WEIGHTS market term
    removed and NO reshape.
  * Realized value = actual season PPR (backtest._load_actual_season).

Reports, per position, eval on 2023/24/25:
  1. Held-out Spearman rank-correlation (flat $  vs realized value).
  2. RANK-INVARIANCE: the same correlation under monotonic reshapes ($**gamma) — proves a
     monotonic within-position reshape CANNOT move per-position rank-correlation.
  3. FLATNESS: top-6 / top-12 $ concentration vs realized-value concentration.
"""
from __future__ import annotations
import warnings; warnings.filterwarnings("ignore")
import pandas as pd

from backend.engines.backtest import _load_actual_season
from backend.engines.valuation import (
    ppr_to_system_value, calculate_replacement_level, get_draftable_pool_sizes,
    POSITION_BUDGET_SHARE, LEAGUE_SKILL_DOLLAR_POOL,
)

PRIOR_WEIGHTS = [0.5, 0.3, 0.2]      # S-1, S-2, S-3 (pipeline weighting)
POSITIONS = ["QB", "RB", "WR", "TE"]
EVAL_SEASONS = [2023, 2024, 2025]
RESHAPE_GAMMAS = [0.7, 1.0, 1.3]     # monotonic reshapes to test rank-invariance


def load_season(yr: int) -> pd.DataFrame:
    df = _load_actual_season(yr)
    namecol = "player_display_name" if "player_display_name" in df.columns else "player_name"
    out = df[[namecol, "position", "fantasy_points_ppr"]].copy()
    out.columns = ["name", "position", "ppr"]
    out["ppr"] = out["ppr"].fillna(0.0).astype(float)
    out["key"] = out["name"].str.lower().str.strip() + "|" + out["position"].astype(str)
    return out.drop_duplicates("key")


def projection_for(eval_yr: int):
    """Leak-free projection: weighted prior realized PPR. Returns (proj Series, pos_of dict)."""
    priors, pos_of = [], {}
    for i, w in enumerate(PRIOR_WEIGHTS, start=1):
        try:
            s = load_season(eval_yr - i)
        except Exception:
            continue
        priors.append((w, s.set_index("key")["ppr"]))
        for _, r in s.iterrows():
            pos_of.setdefault(r["key"], r["position"])
    if not priors:
        return None, {}
    wsum = sum(w for w, _ in priors)
    proj = None
    for w, s in priors:
        term = (w / wsum) * s
        proj = term if proj is None else proj.add(term, fill_value=0.0)
    return proj, pos_of


def flat_board(proj, pos_of) -> dict:
    """Market-free PAR pool-share $ (the flat baseline) per position from the projection."""
    pools = get_draftable_pool_sizes()
    dollars: dict[str, float] = {}
    for pos in POSITIONS:
        keys = [k for k in proj.index if pos_of.get(k) == pos and proj[k] > 0]
        if not keys:
            continue
        pprs = sorted((float(proj[k]) for k in keys), reverse=True)
        repl = calculate_replacement_level(pprs, pools[pos])
        total_par = sum(max(0.0, p - repl) for p in pprs)
        budget = POSITION_BUDGET_SHARE[pos] * LEAGUE_SKILL_DOLLAR_POOL
        for k in keys:
            dollars[k] = float(ppr_to_system_value(float(proj[k]), repl, total_par, budget))
    return dollars


def spearman(a: pd.Series, b: pd.Series) -> float:
    # Spearman == Pearson of ranks (avoids the scipy dependency).
    return float(a.rank().corr(b.rank()))


def topk_share(vals: list[float], k: int) -> float:
    s = sorted(vals, reverse=True)
    tot = sum(s) or 1.0
    return 100.0 * sum(s[:k]) / tot


def realized_par(realized: pd.Series, keys: list[str], pos: str) -> pd.Series:
    """Realized points-above-replacement for a position (same replacement definition as $)."""
    pools = get_draftable_pool_sizes()
    vals = sorted((float(realized[k]) for k in keys), reverse=True)
    repl = calculate_replacement_level(vals, pools[pos])
    return pd.Series({k: max(0.0, float(realized[k]) - repl) for k in keys})


def vpd_by_bucket(d: pd.Series, rpar: pd.Series) -> list:
    """Realized-PAR per dollar by $-rank bucket. Underpricing of elites => top bucket VPD
    HIGHER than lower buckets (you get more realized value per $ buying elites)."""
    order = d.sort_values(ascending=False).index.tolist()
    bounds = [("top6", 0, 6), ("7-18", 6, 18), ("19-36", 18, 36), ("37+", 36, len(order))]
    out = []
    for label, a, b in bounds:
        ks = order[a:b]
        if not ks:
            continue
        sd = sum(d[k] for k in ks); sr = sum(rpar[k] for k in ks)
        out.append((label, sr / sd if sd else 0.0))
    return out


def main() -> None:
    per_pos_corr = {p: [] for p in POSITIONS}
    inv_maxdev = {p: [] for p in POSITIONS}
    flat_shares = {p: [] for p in POSITIONS}
    parreal_shares = {p: [] for p in POSITIONS}
    vpd_rows = {p: [] for p in POSITIONS}

    for S in EVAL_SEASONS:
        proj, pos_of = projection_for(S)
        if proj is None:
            continue
        dollars = flat_board(proj, pos_of)
        realized = load_season(S).set_index("key")["ppr"]
        n_priors = sum(1 for i in range(1, 4) if (S - i) >= 2021)
        matched = sum(1 for k in dollars if k in realized.index)
        print(f"\n=== EVAL {S}  (projection = prior {n_priors} season(s); fit<{S}, eval={S}) ===")
        if matched < 20:
            print(f"  SKIPPED — only {matched} name matches. The {S} actuals use an ABBREVIATED-name "
                  f"feed (e.g. 'k.murray') vs full names in priors; clean matching needs the ID "
                  f"resolver (first-initial+lastname collides). Excluded from the summary.")
            continue
        for pos in POSITIONS:
            keys = [k for k in dollars if pos_of.get(k) == pos and k in realized.index]
            if len(keys) < 8:
                continue
            d = pd.Series({k: dollars[k] for k in keys})
            y = realized.reindex(keys)
            rpar = realized_par(realized, keys, pos)
            c = spearman(d, y)
            per_pos_corr[pos].append(c)
            invs = [spearman(d.pow(g), y) for g in RESHAPE_GAMMAS]
            base = invs[RESHAPE_GAMMAS.index(1.0)]
            inv_maxdev[pos].append(max(abs(v - base) for v in invs))
            fs = topk_share(list(d.values), 12)
            rps = topk_share([rpar[k] for k in keys], 12)   # realized-PAR concentration (apples-to-apples)
            flat_shares[pos].append(fs); parreal_shares[pos].append(rps)
            vpd = vpd_by_bucket(d, rpar); vpd_rows[pos].append(dict(vpd))
            print(f"  {pos}: n={len(keys):3d}  spearman={c:.3f}  reshape{RESHAPE_GAMMAS}->{[round(x,3) for x in invs]}"
                  f"  flat top12$={fs:.0f}% vs realizedPAR top12={rps:.0f}%"
                  f"  VPD/bucket={[(l, round(v,2)) for l,v in vpd]}")

    print("\n" + "=" * 78)
    print("SUMMARY (mean across eval seasons)")
    print("  pos  spearman  rank-invariant(max|dev|)  flat_top12$  realizedPAR_top12  shape_gap")
    for pos in POSITIONS:
        cs = per_pos_corr[pos]
        if not cs:
            continue
        mc = sum(cs) / len(cs); md = max(inv_maxdev[pos])
        fs = sum(flat_shares[pos]) / len(flat_shares[pos])
        rs = sum(parreal_shares[pos]) / len(parreal_shares[pos])
        print(f"  {pos:3s}  {mc:.3f}     {md:.4f}                {fs:4.0f}%        {rs:4.0f}%"
              f"          {rs-fs:+.0f}pp")
    print("\n  VPD (realized-PAR per $) by $-bucket, mean across seasons "
          "[underpriced elites => top6 VPD > 37+ VPD]:")
    for pos in POSITIONS:
        if not vpd_rows[pos]:
            continue
        agg = {}
        for row in vpd_rows[pos]:
            for k, v in row.items():
                agg.setdefault(k, []).append(v)
        means = {k: sum(v) / len(v) for k, v in agg.items()}
        print(f"    {pos:3s}: " + "  ".join(f"{k}={means[k]:.2f}" for k in ["top6", "7-18", "19-36", "37+"] if k in means))
    print("\nREAD: rank-invariant(max|dev|)~0 => per-position Spearman is IDENTICAL for flat and")
    print("any monotonic reshape, so per-position rank-corr CANNOT be the gate. shape_gap>0 =>")
    print("realized PAR more concentrated than $ (too flat); <0 => $ already steeper than realized.")
    print("VPD rising toward top6 => elites underpriced (reshape justified); flat/falling => not.")


if __name__ == "__main__":
    main()
