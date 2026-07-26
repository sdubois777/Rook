# Prospective backtest — as-of 2025-08-15 board vs 2025 actuals

Run 2026-07-25. Row-level results preserved in `asof_2025_backtest_rows.csv` — the board
itself was destroyed by the 2026 restore and the schema has no season dimension, so that
CSV is the only surviving copy of the per-player detail.

```bash
PYTHONUTF8=1 .venv/Scripts/python.exe -c "
import asyncio,logging,json; logging.disable(logging.CRITICAL)
from backend.database import AsyncSessionLocal
from backend.engines.backtest import run_backtest
async def m():
    async with AsyncSessionLocal() as s:
        metrics, df = await run_backtest(s, 2025)
    print(json.dumps(metrics.to_dict(), indent=2, default=str))
asyncio.run(m())"
```

---

## Headline

**The signal is not distinguishable from chance.** 56.9% on 65 calls, one-sided
binomial p = 0.161 against a 50% null. The 95% Wilson interval is 44.8%–68.2% — it
contains 50%. This neither confirms nor refutes that the system has an edge; the sample
is too small to say.

| metric | value |
|---|---|
| signal accuracy | 56.9% (37/65) — p=0.161, 95% CI 44.8–68.2% |
| buy | 57.9% (22/38) — p=0.209 |
| avoid | 55.6% (15/27) — p=0.351 |
| **top opportunities** (`value_gap >= 8`) | **50.0% (14/28)** — p=0.575 |
| projection correlation | 0.771 |
| projection MAE | 43.7 PPR (bias +14.0) |
| within 20% | 24.5% |
| price source | `market_value_historic` (2025, N=159) — the real auction |

Base rate measured on the priced-and-scoreable population: **47.8%**, so the
construction is behaving as designed (`apply_price_relative_outcome` puts residuals
symmetrically about a within-position log-price fit).

The system declined to call 92 of 157 priced players. It made a call on **41%** of the
board.

### Validity

Both earlier numbers named in the handoff are correctly void, and this run is neither:

- 0.904 / 64% was open-book (projections saw 2025).
- 37.3% ran on a half-flagged board against a 2026 market.

Checks that this run was genuinely blind: 0 profiles cite 2025 data (the single grep hit
is `2025` inside a hex fingerprint); 0 beat_reporter_signals rows; board teams are
2025-correct (A.J. Brown PHI, DK Metcalf PIT, Diggs NE, Adams LA — not their 2026 teams);
`latest_season_with_data()` returns 2024 under the clock. The correlation drop from 0.904
to 0.771 is itself consistent with a model that can no longer see the season.

**Top opportunities landing at exactly 50%** is the sharpest result here. Those are the
system's own highest-conviction calls, and they discriminate not at all.

---

## Why: the projections are compressed

Mean projection error against actual finish, within position — a clean monotone gradient:

| actual finish | n | mean bias | projected | actual |
|---|---|---|---|---|
| top 5 | 20 | **−75.9** | 239.1 | 315.0 |
| 6–12 | 28 | −28.0 | 213.4 | 241.4 |
| 13–24 | 48 | −16.0 | 169.8 | 185.8 |
| 25–48 | 96 | +11.2 | 120.0 | 108.9 |
| 49+ | 273 | +31.1 | 63.5 | 32.5 |

Everything is squeezed toward the middle. Rank-5-to-rank-40 spread, actual ÷ projected:

| pos | ratio |
|---|---|
| WR | **1.44×** |
| RB | **1.38×** |
| TE | 1.19× |
| QB | 1.02× |

This drives the failure mode directly. The system said **avoid** on Puka Nacua ($43,
scored 375), Christian McCaffrey ($50, 414), Trey McBride ($39, 316) and Amon-Ra
St. Brown ($51, 324). Compressed projections make every expensive player look overpriced,
so the system systematically avoids the studs — who are expensive because they deliver.

The `+14.0` aggregate bias hides this: it is the average of −76 at the top and +31 at the
bottom.

## Why it matters more: the edge is uncorrelated with the outcome

```
corr(ai_ceiling - price, actual − price_implied_ppr)  =  0.057
```

The quantity every signal is built from has essentially no relationship to whether a
player beat his price slot. Sorting the board by our edge: top quintile beats its slot
52% of the time, bottom quintile 45%.

And within the two positions that decide an auction, **the market is the better
predictor**:

| pos | corr(our ceiling, actual) | corr(auction price, actual) |
|---|---|---|
| RB | 0.645 | **0.719** |
| WR | 0.378 | **0.414** |

Pooled across positions our ceiling looks better (0.497 vs 0.436), but that is Simpson's
paradox — QB has far wider spread than RB/WR and inflates the pooled figure. Within
position, we lose.

`corr(ai_ceiling, league_price) = 0.759`: three-quarters of the ceiling is the market it
is supposed to beat. What is left over is mostly noise.

---

## What this implies for the accuracy work

Ordered by what the measurement actually supports.

1. **Fix the compression — targeted, and already measurable.** The bias gradient is
   monotone and the `run_format_backtest` harness already computes `spread_ratio` and
   fits nothing, so a projection reshape can be scored without a free knob. Target:
   WR 1.44×, RB 1.38×. This is *not* the anchor-and-multiplier fix that was designed and
   rejected — that one was scored on MAE-vs-market, which is the wrong target. MAE
   barely moves under a spread transform; the rank-dependent bias is what moves.

2. **But do not expect (1) alone to lift signal accuracy.** With
   `corr(edge, residual) = 0.057`, rescaling a ranking that carries little information
   cannot manufacture information. Expanding the spread should fix the avoid-the-studs
   failures specifically — the ten worst avoids are all \$36–51 elite players — without
   necessarily moving the headline number. Both should be measured separately.

3. **The sample is the binding constraint on measurement.** 65 calls cannot resolve a
   5-point edge; distinguishing 57% from 50% at p<0.05 needs roughly 270 calls. One more
   as-of season (2024 or 2023, same machinery) would roughly triple it and is the
   cheapest way to make any future change falsifiable.

4. **Projection bucketing persists.** 752 players hold 179 distinct projected totals; 63
   of the 65 scored calls hold 34 distinct values. Named in the handoff, unchanged.

### Caveats

- Beat reporter was skipped entirely (live RSS has no archive), so the board is missing a
  signal a real 2025-08-15 board would have had. Direction of effect unknown.
- 28 skill players outside the priced 159 carried a stale 2026 market value (all ≤\$15).
  None are scored — `run_backtest` gives unpriced players `signal=None` — so the headline
  is unaffected. See `_seed_asof_market`'s docstring.
- `within_20pct` of 24.5% is low but expected given the bucketing.
