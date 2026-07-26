# Signal accuracy recon — what actually moves 57% → 70%

Written 2026-07-25, from the as-of 2025 prospective backtest
(`docs/recon/asof_2025_backtest_result.md`). Every number here is measured on the 151
priced players in `asof_2025_backtest_rows.csv`, or on nflverse seasonal data. Nothing is
estimated from intuition.

**Read this first:** one season of 151 priced players carries a bootstrap 95% CI of
**±8 accuracy points**. No single change below that size can be validated on this data
alone. That constraint shapes every recommendation below.

---

## The answer in one paragraph

The projection is not the bottleneck people assume. **The chain that turns a projection
into a dollar ceiling destroys most of its predictive content**, and the system's
conviction metric is *anti*-correlated with being right. Fixing those two costs no
modelling work and is worth roughly +9 and +10 points respectively on this data. That
plausibly lands you in the mid-60s. Getting from there to 70% needs genuinely better
per-game projections, where we currently sit *below* the auction market. The hard ceiling
is **74.6%**, because half the outcome is availability and availability is not
forecastable among drafted players — I tested that lever thoroughly and it is dead.

---

## The hard ceiling: 74.6%

Decompose the outcome (`actual_ppr` vs what the price predicted):

| component | R² of price-residual |
|---|---|
| games played (availability) | **0.513** |
| per-game scoring rate | 0.266 |
| both | 0.640 |

Availability is the single largest term. And among **drafted** players it is
unforecastable — R² of 2025 games played against every feature available:

| predictor | R² |
|---|---|
| mean prior games | 0.0006 |
| + variance of prior games | 0.0088 |
| + log price | 0.0157 |
| + prior PPR | 0.0287 |

So 51.3% of the outcome variance is noise from our seat. Max achievable correlation with
the residual is `sqrt(1 − 0.513) = 0.698`, which maps to **74.6% accuracy** — and only if
per-game production were predicted *perfectly*.

**70% requires r = 0.588, i.e. 84% of the theoretical maximum.** It is reachable, but it
is not a modest target. We are at r = 0.220 today.

### The availability lever is dead — I nearly recommended it

This looked like the answer and is not. Recording it so nobody re-proposes it.

Prior availability predicts next-season games at **r = +0.633** across all skill players
(n=251) — and that is *not* survivorship: rebuilt without requiring a 2025 row (absence
counted as 0 games), it still reads **+0.617** (n=559). The injury-risk agent already
computes exactly this (`compute_availability_metrics`, 0.5/0.3/0.2 weighting).

But on the 94 **priced** players with full history it reads **−0.019**. Range restriction:
among players good enough to be drafted, who stays healthy next year is a coin flip. The
broad correlation comes from separating real NFL players from fringe ones — a distinction
that does not exist on a draft board. The market also already prices what little there is
(`corr(predictor, log price) = +0.216`; against the games-residual-vs-price, −0.033).

Applied end-to-end it makes things **worse**: 60.3% → 54.3%. Every variant loses
(mean 56.3%, 2024-only 54.3%, min 55.0%, shrunk-to-17 55.6%). The gsis join was verified
sound first — joined 2025 games match the backtest's own column at 100.0%.

---

## Lever 1 — the ceiling destroys the projection's signal (biggest, cheapest)

Using the raw projection versus using `ai_bid_ceiling`, same players, same scoring rule:

| predictor | accuracy (n=151) |
|---|---|
| **raw projection** | **60.3%** |
| `ai_bid_ceiling` | 51.7% |
| raw dollar edge (`ceiling − price`) | 55.6% |
| the shipped `system_signal` | 56.9% |

Paired **exact McNemar p = 0.029** (projection-right/ceiling-wrong 22, reverse 9). And the
ceiling adds **+0.0005 R²** beyond the projection — literally nothing. The entire
PAR → `valuation_agent` chain, which is ~45 minutes of the ~75 minute run, contributes no
predictive value to the signal.

Two mechanisms, both measured:

1. **Market anchoring.** Regressing ceiling on (projection, log price) within position:
   QB β_price = 0.390, TE 0.335, RB 0.166, WR −0.043. For QB and TE the ceiling is
   substantially a restatement of the price it is meant to beat. Overall
   `corr(ceiling, price) = 0.759`.
2. **Quantization.** The ceiling is an integer dollar value and saturates at the bottom:
   in the $0–5 band, 24 distinct projections collapse into **5** distinct ceilings.

Rank correlation survives (spearman 0.96–0.98 at RB/WR/TE), so this is not a reordering —
it is a compressive, market-contaminated rounding that destroys exactly the fine ordering
the residual test needs.

**Do:** derive the buy/avoid signal from the projection's residual against price, not from
the dollar ceiling. Keep `ai_bid_ceiling` as the auction bid surface — it is needed for
bidding — but stop routing the *signal* through it. Alternatively, make the ceiling a
strictly monotone within-position function of the projection with sub-dollar precision and
no market term.

---

## Lever 2 — the conviction metric is backwards

Accuracy when only the highest-conviction calls are made:

| fraction of board called | by **dollar gap** (ships today) | by **projection residual** |
|---|---|---|
| 100% | 60.3% | 60.3% |
| 50% | 60.0% | 58.7% |
| 30% | **51.1%** | 64.4% |
| 20% | **46.7%** | 70.0% |

The system's current conviction measure gets *worse* the more confident it is. That is
precisely why `top_opportunities` scored exactly 50.0% (14/28) in the backtest — the
metric was selecting for the wrong thing.

**Caveat, stated plainly:** the right-hand column is non-monotone below 20% (15% → 63.6%,
10% → 66.7%), which is small-n noise. Do **not** treat "70.0%" as achieved. The reliable
claim is the *sign*: dollar-gap conviction is anti-predictive, residual conviction is
predictive. Both directions are consistent across the whole curve.

**Do:** re-rank conviction on the standardised within-position projection residual, and
make the call/no-call threshold explicit. The system currently calls 41% of the board
(65 of 157); that is a tunable knob nobody has tuned.

---

## Lever 3 — REFUTED. The projections are OVER-dispersed, not compressed

**This section originally recommended expanding projection spread. That recommendation was
wrong, and the error was in this document's own reasoning.** Corrected 2026-07-25 during
implementation.

Two mistakes:

1. **The bias gradient is regression to the mean, not compression.** "Top-5 finishers
   under-projected by −76 PPR, rank-49+ over-projected by +31" conditions on *actual*
   finish. The top actual finishers were, on average, lucky, so **any** unbiased forecast
   under-projects them. A perfect forecast produces that exact gradient. It is not evidence
   of anything.

2. **An optimal forecast is SUPPOSED to be narrower than the outcome.** It is a conditional
   expectation, and `Var(E[X|I]) < Var(X)`. For an optimally scaled forecast,
   `sd(proj)/sd(actual) = corr(proj, actual)`. So "projections are flatter than reality" is
   the expected state.

The correct test is the regression slope of actual on projected — 1.0 means calibrated,
below 1.0 means **over**-dispersed:

| pos | slope | se | sd_proj/sd_act | optimal ratio (= corr) |
|---|---|---|---|---|
| QB | 0.145 | 0.295 | 0.737 | 0.106 |
| RB | 0.989 | 0.156 | 0.672 | 0.665 |
| WR | 0.620 | 0.200 | 0.632 | 0.392 |
| TE | 0.703 | 0.248 | 0.791 | 0.556 |
| **n-weighted** | **0.688** | | | |

Every position sits at or below 1.0, and at every position but RB the actual spread ratio
*exceeds* the optimal one. The projections already spread further than their skill
justifies. Expanding them monotonically worsens MAE (61.5 → 64.3 → 68.3 → 78.3 at
k = 1.0 → 1.2 → 1.4 → 1.77).

**And spread cannot move the signal at all.** A linear rescale multiplies every residual by
the same factor, leaving sign and standardised conviction untouched — measured signal
accuracy is **60.3% at every k from 0.6 to 1.77**. A nonlinear quantile recalibration
(mapping onto the prior-seasons actual shape, no look-ahead) was also built and tested: it
made things *worse*, 60.3% → 56.3%, and degraded the conviction curve from 66.7% to 53.7%
at |z| ≥ 1.0.

This also explains why the earlier anchor-and-multiplier rewrite was rejected in
verification for making MAE worse — same underlying error.

**What actually fixed the stud-avoid failures** was lever 1's price-neutrality, not any
spread change. On the shipped basis Ja'Marr Chase ($65) read `slight_overpay`; on the
conviction basis he reads `elite_value`, and Puka Nacua goes `avoid` → `fair_value`.

**Residual opportunity (not shipped):** shrinking projections toward the position mean
would improve calibration and MAE (k = 0.8 gives 59.9 vs 61.5). It does **nothing** for
signal, and it would move every dollar value on the board, so it needs its own validation.

---

## Lever 4 — the actual wall: per-game projection skill

Within-position correlation with realised per-game rate:

| pos | ours | market |
|---|---|---|
| QB | 0.381 | 0.318 |
| RB | 0.694 | **0.740** |
| WR | 0.559 | 0.566 |
| TE | 0.460 | **0.706** |
| **n-weighted** | **0.566** | **0.607** |

We are *below* the auction market at predicting per-game production. (The pooled 0.684 vs
0.512 figure in the earlier report is Simpson's paradox — cross-position spread inflates
it. Within position, we lose.)

What each skill level is worth, simulated with availability left as noise:

| within-pos corr(proj, ppg) | signal accuracy |
|---|---|
| 0.566 — **us today** | 61.9% |
| 0.607 — the market | 63.6% |
| 0.70 | 66.0% |
| 0.75 | 68.0% |
| **0.80** | **69.4%** |
| 0.85 | 71.6% |

**70% needs within-position per-game correlation of ~0.80** — well beyond both us and the
market. This is the real work, and it is not a plumbing fix. TE (0.460 vs market 0.706) is
the worst gap and the smallest population; QB is near-noise for everyone (market 0.318).

---

## Recommended order

| # | change | measured worth | status |
|---|---|---|---|
| 1 | signal from projection residual, not the dollar ceiling | 55.6% → 60.3% | **SHIPPED** (`signal_basis.py`) |
| 2 | conviction = standardised residual, not dollar gap | top-17%: 48.0% → 64.0% | **SHIPPED** (`players.signal_conviction`) |
| 3 | expand projection spread | none — refuted, see above | **NOT SHIPPED** (harmful) |
| 4 | per-game projection skill 0.566 → 0.80 | the only route past ~64% | open, high cost |
| 5 | run 2–3 more as-of seasons | none directly — makes 1–4 *falsifiable* | open |

**On (5):** with ±8 points of noise on one season, levers 1–3 cannot be individually
validated on 2025 alone. The as-of machinery now works end to end and is committed
(#367–#377), so 2023 and 2024 boards are mostly a matter of compute. Without them you will
be shipping changes you cannot distinguish from noise — which is exactly how the 0.904 and
37.3% numbers happened.

**Realistic expectation:** levers 1–3 plausibly land in the **mid-60s**. 70% requires
lever 4 and is 84% of the theoretical maximum. I would not commit to 70% until at least
one more season confirms levers 1–3 actually hold.

---

## Do not re-propose

- **Availability / games-missed modelling** — refuted above with a direct end-to-end test.
- **Anchor-and-multiplier projection rewrite** — rejected in earlier verification (MAE vs
  market got worse in every variant).
- **Value-at-stake Sonnet routing gate** — lands 15 red tests and silently un-gates the
  #367 double-count fixes.
- **Full duplicate-player dedupe as a migration** — crashes the Railway deploy.

See "Known problems" in `docs/HANDOFF_asof_backtest.md` for the full list with reasons.
