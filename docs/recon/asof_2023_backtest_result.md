# As-of 2023 prospective backtest — result

Run 2026-07-26 against the pre-registration in
`docs/recon/asof_2023_preregistration.md` (committed before the board existed).
Cost **$14.78**, 81 minutes of agent phases. Rows: `docs/recon/asof_2023_backtest_rows.csv`.
Board preserved at `backups/rook_asof2023_board.sql`; the 2026 board was restored and verified.

---

## 1. Standalone — reported before pooling, as pre-registered

**50 of 76 calls = 65.8%.** Exact one-sided p = 0.0040. 95% CI **54.6 – 75.5%**.
The interval's lower bound clears the 50% base rate, so 2023 is significant on its own.

Pre-registered prediction was 60.3% with CI 52.3–67.7% on ~75 calls. The result landed
**inside** the predicted interval, at 76 calls. Break-even for the pooled result to
survive was >= 38/75 (50.7%); actual was 50/76.

| | 2023 |
|---|---|
| buy | 74.5% (n=51) |
| avoid | **48.0% (n=25)** |
| model accuracy (rate-based) | 61.5% on 148 |
| **edge r** | **0.481** |
| projection | mae 44.5, bias +16.7, corr 0.775 |
| price source | `league_auction_history (2023, N=175)`, 154 matched, 21 unmatched (all K/DST) |

**Buys carried it.** Avoids at 48.0% are below a coin flip. The headline is a
buy-side result, not a symmetric one.

---

## 2. Pooled — committed in advance, not conditional on the result

```
2023   50/76 = 65.8%    2024   50/78 = 64.1%    2025   41/73 = 56.2%
POOLED 141/227 = 62.1%   exact two-sided p = 0.00032   95% CI 55.7 - 68.2%
```

Pre-registered break-even was 129/226 = 57.1%. **Cleared.**

Against the previous two-season figure (91/151 = 60.3%, exact two-sided p = 0.0144,
CI 52.3–67.7%), the third season moves the point estimate +1.8 points, cuts the p-value
by ~45x, and narrows the interval from 15.4 to 12.5 points.

---

## 3. The contamination prediction was wrong — in the favourable direction

The pre-registration stated, in advance, that 2023 would be the first board built
without the as-of snap leak (#394) and the first with working depth charts (#393), and
that a **LOWER** number was therefore the expected outcome of removing contamination.

It went **up**, not down. Verified on the board itself: every 2023 rookie carries
`snap_percentage = None` (Bijan Robinson, Jahmyr Gibbs, Anthony Richardson, Zay Flowers,
Jaxon Smith-Njigba), while veterans carry real 2022 values (Jefferson 0.924, Kelce 0.798,
Allen 0.979). On the old boards the equivalent rookies held the OUTCOME season's share
(Bo Nix 0.989, Caleb Williams 0.988).

This is consistent with the adversarial finding that the leak's linear channel explained
only ~5–11% of the orthogonal signal. **The 60.3% was not being propped up by the leak.**

---

## 4. The one "ORTHOGONAL" result is NOT a win — the sign reverses

`dep_displaced` and `dep_flag_count` both cleared |t| >= 2 on this board
(t = 2.46 / 2.10, stability 0.99 / 0.978), under the stricter nonlinear price control
shipped in #394. That is the project's founding thesis — and it does not hold up:

| season | beta | t | stability |
|---|---|---|---|
| 2023 | **+0.1561** | **+2.46** | 0.99 |
| 2024 | −0.1123 | −1.54 | 0.955 |
| 2025 | −0.0531 | −0.69 | 0.797 |

**The sign reverses.** A negative coefficient means displaced players underperform their
price — the thesis. 2023's positive coefficient says the opposite. A signal that clears
the bar in one season with the reverse sign to the other two is the
`injury_projected_games` pattern (clears in a different season each time), not a lead.

Under this project's own discipline, do not build on this. It needs a fourth season, or
a mechanism explaining why 2023 would invert.

---

## 5. What is NOT established

- **2023 is not strictly comparable to 2024/2025.** It is the only board with working
  depth charts, correctly-seasoned team grades, an id-keyed price join, and no snap
  leak. The +9.6 points over 2025 could be board quality or season variance and this
  design cannot separate them. Do NOT attribute it to the code fixes — the standing
  rule is that a between-season change is never evidence about code.
- **The pool mixes code versions.** Three boards built at three different SHAs, only the
  third of which is clean. Pooling was pre-committed, so the number is honest, but it is
  not three draws from one process.
- **Nothing has been shown to beat the market.** Signal accuracy is measured against a
  within-position ln(price) residual, so 62.1% means the *calls* discriminate. The
  separate budget-constrained draft simulation against a real preseason-ADP control
  found no detectable difference from the market, and that is unchanged by this run.
- `top_opportunities` still selects on the dollar gap, not `signal_conviction`
  (`backtest.py:948`). Known defect, unfixed, and it was disclosed at freeze time.

---

## 6. Owed follow-ups

1. Update `docs/recon/signal_accuracy_state.md` — it still reports the two-season
   60.3% as authoritative, plus the six corrections itemised in
   `signal_accuracy_audit_report.md` §3.3.
2. Re-run the orthogonality table for 2024/2025 under the new nonlinear price control
   and replace the stale published t-values.
3. `top_opportunities` -> conviction, with a pre-registered cut.
4. A fourth season is the only way to resolve the `dep_displaced` sign reversal.
