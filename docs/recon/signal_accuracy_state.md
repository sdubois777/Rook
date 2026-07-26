# Signal accuracy — current state

Written 2026-07-25, at the end of the as-of backtest + signal-accuracy work
(PRs #367–#389). This is the authoritative summary; the other recon docs are the
working detail behind it.

---

## Where accuracy actually is

**Decision accuracy: 60.3%** — 91 of 151 calls across two prospective seasons,
p = 0.0059, 95% CI **52.3–67.7%**. Base rate is ~50%, so this is the first
statistically significant evidence the system has an edge.

| season | decision | calls | model (per-game) | edge r | price source |
|---|---|---|---|---|---|
| 2025 | 56.2% | 73 | 63.8% | 0.222 | market_value_historic (N=159) |
| 2024 | 64.1% | 78 | 60.1% | 0.296 | league_auction_history (N=177) |

The two seasons disagree by 8 points, which is why the interval is 15 points wide.
"60%" is the point estimate; the honest range is high-50s to mid-60s.

`top_opportunities` — the calls you would actually act on — is **56.1% pooled**
(37/66, p = 0.195). Not significant. The high-conviction slice is not yet better
than the board average.

---

## THE CODE CHANGES DID NOT IMPROVE ACCURACY

Controlled re-run, same season and same prices, code the only variable:

```
2025 OLD code : 56.9% on 65 calls
2025 NEW code : 56.2% on 73 calls
```

So the 2024 figure is **season variance, not code**. Do not cite 64.1% as validation
of anything shipped.

What did ship is real but not an accuracy gain: working instrumentation, correct
units, a price-neutral conviction basis, and several silent-failure fixes.

**Rule this bought:** never attribute a between-season accuracy change to code —
re-run the SAME season on both versions. And check any projection change against MAE
and bias, not just accuracy: the signal is sign-based and scale-invariant, so a level
regression is invisible to it. That is exactly how the ×17 scaling regression hid.

---

## The ceiling, and what 75% would need

Availability is **51.3%** of the price-residual variance and is unforecastable among
drafted players (R² 0.0006–0.029 against every feature tried). That caps edge
correlation at 0.698:

| coverage | accuracy needed for 75% | r required |
|---|---|---|
| every call (~155) | — | **impossible** (needs r > 0.71 vs a 0.70 max) |
| top 30% (~46 calls) | 75% | r = 0.40 |
| top 20% (~31 calls) | 75% | r = 0.36 |

We are at **r ≈ 0.22–0.30**. The best measured candidate (dependency flags) moves it
to at most 0.238 in-sample, which is an upper bound. **There is no known path to 75%
with current signals.**

Realistic near-term target: ~70% on the top 20%, which needs r ≈ 0.30.

---

## The orthogonality rule

The outcome is a residual against a within-position `ln(price)` fit, so anything the
market already knows is worth **exactly zero**. Measured: blending our projection with
market price raises correlation with realised per-game rate 0.566 → 0.630 while signal
accuracy stays at 60.3% **at every blend weight**. That is algebra, not noise.

`measure_orthogonality()` in `backend/engines/backtest.py` runs on every backtest. Put
any new signal through it before building on it.

Results so far (two seasons, and they disagree):

| candidate | 2025 | 2024 |
|---|---|---|
| `dep_displaced` / `dep_contingent` | t −1.83 / −1.76, 97–98% stable | t −1.48, 94% |
| `injury_projected_games` | t −0.39 (nothing) | **t −2.49, 99% — cleared the bar** |
| `dep_net_impact` | t −0.09 → t −1.81 after the unit fix | t +0.79 |
| `beat_signal_count` | untestable (no signals) | ~0 |

Nothing is established. `injury_projected_games` clearing in one season and measuring
nothing in the other is the clearest example of why one season is not enough.

---

## Refuted — do not re-propose

- **Availability / games-missed modelling.** Prior games predicts next-season games at
  r = +0.633 across all skill players (+0.617 with no survivorship) but **−0.019** among
  the 94 priced players. Range restriction, and the market already prices it. Applied
  end to end it made accuracy WORSE: 60.3% → 54.3%.
- **Expanding projection spread.** The "compression" diagnosis was regression to the
  mean — conditioning on actual finish makes any unbiased forecast look like it
  under-projects the top. Calibration slope is 0.688 (≤1 everywhere), so projections
  are already OVER-dispersed. Expanding worsens MAE and cannot move a sign-based signal
  (60.3% at every multiplier 0.6×–1.77×). A nonlinear quantile version made it worse.
- **Blending in consensus/ADP/expert ranks** — measured zero, see the orthogonality rule.
- **Anchor-and-multiplier projection rewrite**, **value-at-stake Sonnet routing gate**,
  **full duplicate-player dedupe as a migration** — all rejected earlier; see
  `docs/HANDOFF_asof_backtest.md`.

---

## What shipped (#367–#389)

- **As-of prospective backtesting** via `ROOK_ASOF_DATE`, end to end.
- **Signals from the projection, not the dollar ceiling** (#378). The ceiling scored
  51.7% vs the projection's 60.3% (paired McNemar p = 0.029) and added +0.0005 R².
  New `players.signal_conviction` is price-neutral (corr with ln price +0.017 vs −0.581)
  and is what opportunity ranking sorts on — the dollar gap scored 46.7% in the top 20%.
- **Orthogonality harness** (#379).
- **Model vs decision accuracy** reported separately (#382). Rate-based scoring drops
  availability's share of residual variance 0.513 → 0.039, ceiling 74.6% → 93.7%, so
  projection improvement is actually detectable. Reported alongside, never instead.
- **Dependency flags sized from measured share** (#383, #386): 0.258 × vacated share
  (t +2.91), 0.622 × arrival share for dilution (t −10.84, proportional not flat).
  Fixed a unit bug where `value_impact_pct` held fractions and percentages in the same
  column, ~100× apart.
- **Projections scale to EXPECTED games (14.6), not 17** (#386) — see the regression note.
- **Auction history usable again** (#381) + an importer; 2023/2024 are now scoreable.
- **Three silent as-of failures** (#384) and **a pipeline-aborting KeyError** (#388).

---

## Highest-value next steps

1. **A third priced season.** Everything above rests on two seasons that disagree by 8
   points. 2023 prices are imported and scoreable; the board is a ~$10, ~75 min run.
2. **Make the high-conviction slice actually better.** `top_opportunities` at 56.1%
   pooled is the gap between "has an edge" and "is useful" — you act on ~16 roster
   spots, not 155.
3. **Find price-orthogonal information.** Nothing currently measured clears the bar
   in both seasons. Beat-reporter signals remain the only wholly untested candidate and
   probably cannot be tested retrospectively — there is no archive.
