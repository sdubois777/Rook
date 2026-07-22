#!/usr/bin/env python
"""Phase 2 stress-test — does an RB-only reshape's held-out rank-corr gain survive splits?
READ-ONLY; no DB writes. Every number reproduces here:

    .venv/Scripts/python.exe scripts/reshape_phase2_rb_stress.py

Applies a REAL RB reshape (PAR**gamma renormalized to the RB budget; gamma>1 steepens the
top, the direction the "RB under-serves the top" claim wants) and measures the held-out RB
Spearman for flat vs reshaped across EVERY available fit/eval split (each eval season ×
each prior-window). Also reports a spread-ratio per position to check the "RB uniquely too
flat (0.71)" claim, and RB value-per-dollar by bucket for flat vs reshaped.
"""
from __future__ import annotations
import warnings; warnings.filterwarnings("ignore")
import statistics
import pandas as pd

from scripts.reshape_phase1_baseline import load_season, spearman, realized_par
from backend.engines.valuation import (
    ppr_to_system_value, calculate_replacement_level, get_draftable_pool_sizes,
    POSITION_BUDGET_SHARE, LEAGUE_SKILL_DOLLAR_POOL,
)

POSITIONS = ["QB", "RB", "WR", "TE"]
GAMMAS = [1.3, 1.5, 1.8]          # steepen RB top (the claimed direction). gamma=1.0 == flat.


def projection(eval_yr: int, n_prior: int):
    """Weighted prior-production projection using exactly n_prior seasons before eval_yr."""
    w = [0.5, 0.3, 0.2][:n_prior]
    priors, pos_of = [], {}
    for i, wi in enumerate(w, start=1):
        yr = eval_yr - i
        if yr < 2021:
            continue
        s = load_season(yr)
        priors.append((wi, s.set_index("key")["ppr"]))
        for _, r in s.iterrows():
            pos_of.setdefault(r["key"], r["position"])
    if not priors:
        return None, {}
    wsum = sum(wi for wi, _ in priors)
    proj = None
    for wi, s in priors:
        term = (wi / wsum) * s
        proj = term if proj is None else proj.add(term, fill_value=0.0)
    return proj, pos_of


def rb_dollars(proj, pos_of, gamma: float) -> dict:
    """RB $ via PAR**gamma renormalized to the RB budget (gamma=1.0 -> flat pool-share)."""
    pools = get_draftable_pool_sizes()
    keys = [k for k in proj.index if pos_of.get(k) == "RB" and proj[k] > 0]
    pprs = sorted((float(proj[k]) for k in keys), reverse=True)
    repl = calculate_replacement_level(pprs, pools["RB"])
    budget = POSITION_BUDGET_SHARE["RB"] * LEAGUE_SKILL_DOLLAR_POOL
    raw = {k: max(0.0, float(proj[k]) - repl) ** gamma for k in keys}
    tot = sum(raw.values()) or 1.0
    return {k: max(1.0, raw[k] / tot * budget) for k in keys}


def main() -> None:
    splits = []
    for eval_yr in (2023, 2024):                      # 2025 = abbreviated-name feed, unmatchable
        for n_prior in (1, 2, 3):
            if eval_yr - n_prior < 2021:
                continue
            splits.append((eval_yr, n_prior))

    print("=== 1. RB held-out rank-corr: flat vs reshaped, EVERY split (gain must be > 0 to be real) ===")
    print("  eval  priors  n_RB   spearman_flat   " + "  ".join(f"g={g}" for g in GAMMAS) + "   max_gain")
    gains = []
    for eval_yr, n_prior in splits:
        proj, pos_of = projection(eval_yr, n_prior)
        realized = load_season(eval_yr).set_index("key")["ppr"]
        flat = rb_dollars(proj, pos_of, 1.0)
        keys = [k for k in flat if k in realized.index]
        if len(keys) < 8:
            print(f"  {eval_yr}    {n_prior}      <8 matches — skipped")
            continue
        y = realized.reindex(keys)
        c_flat = spearman(pd.Series({k: flat[k] for k in keys}), y)
        row_corrs = []
        for g in GAMMAS:
            resh = rb_dollars(proj, pos_of, g)
            c = spearman(pd.Series({k: resh[k] for k in keys}), y)
            row_corrs.append(c)
        max_gain = max(c - c_flat for c in row_corrs)
        gains.append(max_gain)
        print(f"  {eval_yr}    {n_prior}      {len(keys):3d}    {c_flat:.4f}        "
              + "  ".join(f"{c:.4f}" for c in row_corrs) + f"    {max_gain:+.4f}")

    if gains:
        print(f"\n  RB reshape rank-corr gain across {len(gains)} splits: "
              f"min={min(gains):+.4f}  max={max(gains):+.4f}  mean={statistics.mean(gains):+.4f}")
        print("  (A monotonic within-RB reshape preserves RB ranks -> gain is identically ~0 on")
        print("   every split. This is why 'RB rank-corr 0.68 -> 0.72' cannot be reproduced.)")

    print("\n=== spread-ratio per position: CV(flat $) / CV(realized PAR)  [<1 => flat under-spread] ===")
    # Use eval=2024, 3 priors (richest split).
    proj, pos_of = projection(2024, 3)
    realized = load_season(2024).set_index("key")["ppr"]
    pools = get_draftable_pool_sizes()
    for pos in POSITIONS:
        keys = [k for k in proj.index if pos_of.get(k) == pos and proj[k] > 0 and k in realized.index]
        if len(keys) < 8:
            continue
        pprs = sorted((float(proj[k]) for k in keys), reverse=True)
        repl = calculate_replacement_level(pprs, pools[pos])
        total_par = sum(max(0.0, p - repl) for p in pprs)
        budget = POSITION_BUDGET_SHARE[pos] * LEAGUE_SKILL_DOLLAR_POOL
        flat = {k: float(ppr_to_system_value(float(proj[k]), repl, total_par, budget)) for k in keys}
        rpar = realized_par(realized, keys, pos)
        def cv(vals):
            m = statistics.mean(vals)
            return statistics.pstdev(vals) / m if m else 0.0
        ratio = cv(list(flat.values())) / cv([rpar[k] for k in keys]) if cv([rpar[k] for k in keys]) else 0.0
        print(f"  {pos}: CV(flat$)={cv(list(flat.values())):.2f}  CV(realizedPAR)={cv([rpar[k] for k in keys]):.2f}  ratio={ratio:.2f}")

    print("\n=== what the RB reshape DOES move: RB value-per-dollar by $-bucket (flat vs gamma=1.5) ===")
    proj, pos_of = projection(2024, 3)
    realized = load_season(2024).set_index("key")["ppr"]
    keys = [k for k in rb_dollars(proj, pos_of, 1.0) if k in realized.index]
    rpar = realized_par(realized, keys, "RB")
    for label, g in (("flat", 1.0), ("reshaped g=1.5", 1.5)):
        d = pd.Series({k: rb_dollars(proj, pos_of, g)[k] for k in keys})
        order = d.sort_values(ascending=False).index.tolist()
        buckets = [("top6", 0, 6), ("7-18", 6, 18), ("19-36", 18, 36), ("37+", 36, len(order))]
        vpd = []
        for lab, a, b in buckets:
            ks = order[a:b]
            sd = sum(d[k] for k in ks); sr = sum(rpar[k] for k in ks)
            if ks:
                vpd.append(f"{lab}={sr/sd:.2f}" if sd else f"{lab}=NA")
        print(f"  {label:16s}: " + "  ".join(vpd))
    print("\n  READ: if reshaping LOWERS top6 VPD further (elites already the worst value per $),")
    print("  the reshape makes the board worse on the magnitude metric while not moving rank at all.")


if __name__ == "__main__":
    main()
