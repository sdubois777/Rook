# Signal accuracy — current state

Rewritten 2026-07-26 after the as-of 2023 run and the measurement audit (#392–#397).
This supersedes the 2026-07-25 version, which reported a two-season 60.3% measured on
contaminated boards with four broken instruments. **This is the authoritative summary.**

---

## Where accuracy is

**62.1% — 141 of 227 calls across three prospective as-of seasons.**
Exact two-sided p = **0.00032**. 95% CI **55.7 – 68.2%**. Base rate ~50%.

| season | decision | calls | 95% CI | model (per-game) | edge r | price source |
|---|---|---|---|---|---|---|
| 2023 | **65.8%** | 76 | 54.6–75.5 | 61.5% | **0.481** | league_auction_history (N=175) |
| 2024 | 64.1% | 78 | 53.0–73.9 | 60.1% | 0.282 | league_auction_history (N=177) |
| 2025 | 56.2% | 73 | 44.8–67.0 | 63.8% | 0.213 | market_value_historic (N=159) |

2023 was pre-registered before the board was built
(`asof_2023_preregistration.md`) and reported standalone before pooling. It landed
inside the predicted interval (60.3%, CI 52.3–67.7%).

**Only 2023 is a clean board.** It is the first built without the snap leak, with working
depth charts, correct team grades and an id-keyed price join. So 2023-vs-2024/2025
differences confound board quality with season variance and **must not be attributed to
code**. The standing rule holds: a between-season change is never evidence about code —
re-run the SAME season on both versions.

### Top opportunities — the slice you would act on

Ranked on `signal_conviction`, a pre-registered top 20%:

| season | board average | dollar gap ≥ 8 (retired) | **conviction top 20%** |
|---|---|---|---|
| 2023 | 49.4% | 72.7% (n=33) | **76.7%** (n=30) |
| 2024 | 50.0% | 58.3% (n=36) | **69.0%** (n=29) |
| 2025 | 49.3% | 55.9% (n=34) | **70.0%** (n=30) |

**Pooled 71.9% vs 62.1% for the dollar gap, on a 49.6% base rate.** Conviction wins in
every season. The previous version of this doc reported 56.1% and concluded the
high-conviction slice was no better than the board average — that figure was measured on
the **dollar gap**, the basis #378 retired, and mixed two code versions. The ~70% target
was already being met; we were measuring the wrong slice.

---

## Projection accuracy — a separate axis

Absolute points, not price-relative. These two do **not** move together: blending market
price into the projection raised correlation with realised points 0.566 → 0.630 while
signal accuracy stayed at 60.3% at *every* blend weight.

| season | n | MAE | bias | corr | within 20% |
|---|---|---|---|---|---|
| 2023 | 427 | 44.5 | +16.7 | 0.775 | 26.7% |
| 2024 | 461 | 43.7 | +12.6 | 0.799 | 23.4% |
| 2025 | 429 | 45.8 | +19.2 | 0.774 | 24.0% |

**We over-project by +13 to +19 PPR every season, and it is entirely the Sonnet path:**

```
sonnet_projection   n=1261   bias +16.2   MAE 44.0
nfl_history         n=  49   bias  -2.5   MAE 50.3
```

Calibration slope is 0.83–0.92, so it is multiplicative. An OLS recalibration takes
pooled MAE 44.6 → 41.8 and helps every season individually (−2.48 / −2.65 / −3.48), so it
transfers rather than overfits. **Not yet shipped** — projections feed `ai_bid_ceiling`
and the frozen `projected_ppr < 80` signal threshold, so it propagates and needs a board
rebuild to validate.

By position, priced players, pooled: QB is the weak spot (MAE 75.9, bias +28.4,
**corr 0.332**); RB best (corr 0.641); WR 0.522; TE 0.606.

Against the market's price-implied projection on priced players we are roughly at parity
— better in 2023 (MAE 53.2 vs 59.5), worse in 2024 and 2025. Note that benchmark is
fitted on the outcome season, so it has hindsight our projection does not.

---

## We have not shown we beat the market

Separate measurement: a budget-constrained draft simulation, exact knapsack, every
strategy optimising its own projection under $200 and roster constraints, scored on
actual points, against a **real preseason FantasyPros ADP control** (week 0, dated days
before kickoff).

```
2024   SYSTEM - MARKET  +218
2025   SYSTEM - MARKET  -126        pooled +46 per season, null sd ~205
```

Three different specifications of the control all reached the same verdict: **no
detectable difference.** Resolving a ~50-point effect needs roughly 10 seasons.

Signal accuracy says our *calls* discriminate against price. It does not say we would
have drafted a better team. Those are different claims.

---

## The orthogonality rule

The outcome is a residual against a within-position `ln(price)` fit, so anything the
market already knows is worth **exactly zero**. That is algebra, not opinion.

`measure_orthogonality()` now controls for price **nonlinearly** — `ln(price)`,
`ln(price)²` and within-position price rank — in both the fit and the collinearity
guard. Before #394 it controlled only for linear `ln(price)`, and `ln(price)²` — a pure
function of the price — scored t = 1.80 with 96.3% sign stability and was reported as
SUGGESTIVE. Every verdict predating #394 was computed on the weaker control.

Results under the corrected harness:

| candidate | 2023 | 2024 | 2025 |
|---|---|---|---|
| `dep_displaced` | **+2.46 ORTHOGONAL** | −1.54 | −0.69 |
| `dep_flag_count` | **+2.10 ORTHOGONAL** | — | — |
| `injury_projected_games` | −1.47 | **−2.15 ORTHOGONAL** | −0.83 |
| `dep_net_impact` | −1.20 | +0.89 | −1.67 |

**Nothing clears in more than one season, and `dep_displaced` reverses sign.** A negative
coefficient means displaced players underperform their price — the founding thesis. 2023
says the opposite. Clearing the bar in one season with the reverse sign to the other two
is noise, not a lead. `dep_contingent` was dropped from `CANDIDATE_SIGNALS`: it is the
same column as `dep_displaced` (corr 1.0000 / 0.9665) and reporting both made one signal
look like two agreeing.

---

## The ceiling

Availability is ~51% of price-residual variance and is unforecastable among drafted
players (R² 0.0006–0.029 against every feature tried), capping decision accuracy near
**74.6%**. We are at 62.1%. Note the bound is 2025-only (2024 computes to 75.9%) and
assumes the predictor is uncorrelated with realised games.

---

## Refuted — do not re-propose

- **Availability / games-missed modelling.** End to end 60.3% → 54.3%; confirmed as a
  projection multiplier on both seasons (McNemar p = 0.041 / 0.049).
- **Expanding projection spread.** Sign-invariant by algebra at every multiplier
  0.6×–1.77×; MAE worsens. Calibration slope ≤ 1 everywhere — already over-dispersed.
- **Blending in consensus / ADP / expert ranks.** Exactly zero for anything affine in
  `ln(price)`, verified in both seasons.
- **Anchor-and-multiplier projection rewrite**; **value-at-stake Sonnet routing gate**
  (demotion deletes the forward projection); **full duplicate dedupe as a migration**
  (Railway runs `alembic upgrade head` at boot — guarded script only).
- **Beating random rosters as evidence of skill.** Trivial: any sensible ranking under a
  budget beats random allocation, and the market's own projection does it too.
- **A 4-thesis, 18-candidate adversarial search for new edge sources** returned zero
  survivors. Late-information, causal-structure, market-microstructure and
  measurement-power theses were all searched; every candidate failed orthogonality,
  existence, testability or power.

---

## Instrument defects fixed (#392–#397)

Each exited 0 and produced a plausible-looking board:

| defect | effect |
|---|---|
| price join keyed by name | 4 skill prices lost in 2024 (Marvin Harrison $40, Deebo Samuel $29); a duplicate "Kenneth Walker" row took the RB's price AND his actuals, emitted avoid, scored **correct** |
| depth charts | 0 rows for 2023/2024 — the 2024 board had no depth signal at all |
| as-of snap leak | outcome-season snap share in the projection prompt, 92.4% / 92.8% coverage |
| orthogonality guard | credited nonlinear transforms of price as new information |
| team grades | all 32 written to leftover 2026 rows via `max(season_year)` |

**The snap leak was not inflating the result.** The pre-registration predicted, in
advance, that removing it should *lower* accuracy. 2023 came in at 65.8%, the highest of
the three.

---

## Next

1. **Build the as-of 2022 board.** Prices imported (174 rows, 154 scoreable, $2309).
   Breaks the `dep_displaced` sign tie and gives two directly comparable clean boards.
   ~$15–18, ~2.5h.
2. **Ship the projection recalibration** — pooled MAE −2.9, consistent across seasons.
   Needs a rebuild to validate because it propagates into dollars and a frozen threshold.
3. **Re-run 2024/2025 orthogonality** under the corrected nonlinear control and replace
   any remaining pre-#394 t-values quoted elsewhere.
