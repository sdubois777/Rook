# Pre-registration — as-of 2023 prospective backtest

**Committed 2026-07-26, BEFORE the 2023 board was built.** Nothing in this file may be
edited after the 2023 result is seen. If something here turns out to be wrong, record
the deviation in the results write-up — do not amend this document.

Frozen at **`71efead25f2e1677d8114f7427f77145dfc9f360`** (develop, after #392/#393/#394).

## Why this exists

The pooled 60.3% carries an unusually large number of researcher degrees of freedom:
seven hardcoded signal thresholds, eight candidate outcome definitions, a max-of-4
choice of signal basis made on the 2025 board, and a choice of price surface. The audit
measured the threshold grid at 54.55–65.32% and found the shipped configuration sits at
the 31.8th percentile of it — i.e. NOT tuned to maximise. That is exculpatory, but it
was established retrospectively. A third season is only worth $20 if the answer cannot
be steered after the fact.

---

## 1. Frozen parameters — by name AND value

Changing any of these after seeing the 2023 result invalidates the run.

### `backend/engines/backtest.py`

| constant | value |
|---|---|
| `FAIR_VALUE_PPR_PER_DOLLAR` | 3.8 |
| `_BUY_ASSESSMENTS` | `{"elite_value", "good_value"}` |
| `_AVOID_ASSESSMENTS` | `{"avoid", "slight_overpay"}` |
| `MIN_PRICE_COVERAGE` | 50 |
| `RATE_MIN_GAMES` | 4 |
| `FULL_SEASON_GAMES` | 17 |
| `ORTHOGONALITY_T_THRESHOLD` | 2.0 |
| `ORTHOGONALITY_T_SUGGESTIVE` | 1.5 |
| `ORTHOGONALITY_STABLE_SHARE` | 0.95 |
| `ORTHOGONALITY_BOOTSTRAP` | 400 |
| `ORTHOGONALITY_MIN_N` | 30 |
| `ORTHOGONALITY_MIN_INDEPENDENT_SHARE` | 0.01 |

`derive_system_signal` thresholds, all seven:

```
pay_up_flag              -> strong_buy, unconditionally
league_price <= 12       -> never avoid
projected_ppr < 80       -> never avoid
-8 <= value_gap <= 0     -> neutral band
value_gap >= 5           -> strong_buy (with a buy assessment), else buy
value_gap <= -8          -> avoid  (with a confirming assessment)
value_gap <= -15         -> strong_avoid
```

Grade bands: `>= 65 STRONG`, `>= 55 MODERATE`, `>= 45 WEAK`, else `POOR`.

`apply_price_relative_outcome` is frozen **in full**: within-position least-squares fit
of `actual_ppr ~ a + b*ln(league_price)`, outcome = sign of the residual, minimum 4
players and 2 distinct prices per position, unscoreable rows excluded.

### `backend/engines/signal_basis.py`

| constant | value |
|---|---|
| `CONVICTION_STRONG` | 1.25 |
| `CONVICTION_WEAK` | 0.50 |

### Price source

Precedence frozen as implemented: `league_auction_history` keyed by `player_id`, then
by `player_name` for rows lacking an id, then `market_value_historic` by `player_id`,
then abort to the `market_value_league` sentinel.

**The league auction is the SOLE primary benchmark.** `players.adp_fantasypros` is
**not** an admissible benchmark for the headline — it is byte-identical across the 2024
board, the 2025 board and the live 2026 board (a single 2026 scrape). No alternative
price surface may be substituted after seeing the result.

---

## 2. The metric

**Primary: decision accuracy over CALLED rows** — `system_correct.notna() & actual_ppr.notna()`.

`neutral` rows remain **excluded** from the denominator. This is frozen deliberately:
the 156 neutral rows across 2024+2025 beat their price slot only 42.3% of the time
against a 48.9% base rate, and scoring them as avoids would produce ~57.7%. That may be
real discrimination or an artifact of the price-conditioned suppression rules — it is
untested, and choosing it now, after seeing that it flatters us, would be exactly the
degree of freedom this document exists to close.

Reported alongside, never instead of:
- model accuracy via `score_on_rate()`
- the price-orthogonal residual correlation r
- `top_opportunities`, **relabelled as the dollar-gap slice** — it selects on
  `value_gap >= 8`, not on `signal_conviction`, and that is a known defect not yet fixed

---

## 3. The prediction

Stated before the data exists:

```
point estimate    60.3%
95% CI            52.3 - 67.7%
expected calls    ~75
```

**Break-even.** For the pooled result to remain significant at exact two-sided p < 0.05,
2023 must return **>= 38 of ~75 calls (50.7%)**. Pooled n=226 needs 129 successes (57.1%).

Probability the headline dies, by true rate: **0.040 at 60%, 0.192 at 55%, 0.500 at 50%.**

A 2023 result below 50.7% is a real outcome, not a bug to be debugged into compliance.

---

## 4. Report 2023 STANDALONE before pooling

This is the last remaining degree of freedom and it is closed here.

The 2023 number will be written down and reported on its own **before** any pooled
figure is computed. Pooling is committed to in advance — it is not conditional on the
2023 result being favourable.

Null: season × position × price-band stratified permutation, plus a 2×2 Fisher on
buy-correct vs avoid-correct.

---

## 5. Known defects present at freeze time

Recorded so they are not discovered afterward and used to explain away a bad result:

- **`top_opportunities` measures the dollar gap**, not conviction (`backtest.py:948`).
- **Both prior boards are contaminated** by the as-of snap leak (92.4% / 92.8% coverage).
  Fixed in #394, so the 2023 board is the FIRST clean one — which means 2023 is not
  strictly comparable to 2024/2025 and a drop could be the contamination coming out.
  **This asymmetry is predicted here, in advance.**
- **Depth charts were absent from the 2024 board** (0 rows; fixed in #393). The 2023
  board will have them. Same non-comparability caveat.
- The 2024/2025 boards were built at unknown SHAs — there is no provenance column.

Because of the first three, a 2023 result BELOW 2024/2025 is consistent with the earlier
boards having been flattered, and must not be read as the system getting worse.

---

## 6. Cost and abort conditions

Budget **$20**, wall clock ~3h end to end.

Staged: seed → `team_systems` → `roster_changes` → **gate** → `player_profiles` onward.
The gate must pass before the expensive stage is released. Abort conditions:

```sql
SELECT count(*) FROM players WHERE depth_chart_order IS NOT NULL;   -- >= 400
SELECT season_year, count(*) FROM team_systems GROUP BY 1;          -- (2023, 32)
SELECT season_year, count(*) FROM player_dependencies GROUP BY 1;   -- 2023 only
SELECT count(*) FROM players WHERE market_value_fantasypros IS NOT NULL;  -- ~175
SELECT count(*) FROM players p JOIN league_auction_history h
  ON h.player_id = p.id AND h.season_year = 2023
 WHERE p.market_value_fantasypros IS DISTINCT FROM h.price;         -- 0
```

The 2026 board is preserved at `backups/rook_2026_board_pre2023run.sql`
(32,144,138 bytes, 29 tables, taken 2026-07-26 immediately before this run).
