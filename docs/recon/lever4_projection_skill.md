# Lever 4 — what is actually needed to reach 70%

Follow-on to `docs/recon/signal_accuracy_recon.md`, written 2026-07-25 after levers 1
and 2 shipped (#378). Measured on the same 151 priced players from the as-of prospective
backtest, plus nflverse seasonal data. Everything uses pre-2025 inputs only.

---

## The single most important finding: the target is NOT "raise the correlation"

The earlier recon framed lever 4 as "get within-position per-game correlation from 0.566
to ~0.80". That framing is incomplete in a way that would waste the entire effort.

Blending our projection with the market price:

| weight on market | corr with true per-game rate | **signal accuracy** |
|---|---|---|
| 0.0 (ours) | 0.566 | **60.3%** |
| 0.2 | 0.599 | **60.3%** |
| 0.4 | 0.621 | **60.3%** |
| 0.6 | 0.630 | **60.3%** |
| 0.8 | 0.626 | **60.3%** |
| 1.0 (pure market) | 0.609 | 54.3% |

Projection accuracy improves by a lot. **Signal accuracy does not move by one basis
point.** This is exact, not noisy: the outcome is a residual against `ln(price)`, and
adding any multiple of `ln(price)` to the projection is absorbed by the very fit that
defines the residual. It cancels algebraically.

So **any** work that improves the projection by importing information the market already
has — consensus projections, ADP, expert ranks, "blend in FantasyPros", ensembling with
another public source — is worth **exactly zero** to the signal, no matter how much it
improves MAE or correlation.

What *does* move it is skill orthogonal to price:

| price-orthogonal skill added (sd) | corr with per-game | signal accuracy |
|---|---|---|
| 0.0 | 0.566 | 60.3% |
| 0.1 | 0.632 | 62.3% |
| 0.2 | 0.690 | 62.9% |
| 0.3 | 0.739 | 67.5% |
| 0.4 | 0.779 | 68.2% |
| **0.5** | 0.811 | **69.5%** |

**The requirement for 70% is roughly 0.5 sd of genuinely price-orthogonal predictive
skill about per-game production.** That is the whole job. Nothing else counts.

---

## Where we stand against a trivial baseline

Within-position correlation with realised 2025 per-game rate:

| predictor | n | weighted r |
|---|---|---|
| the auction market price | 151 | **0.608** |
| our projection (the Sonnet machinery) | 151 | 0.566 |
| naive: 2024 per-game alone | 129 | 0.534 |
| naive: best of 2023/2024 | 130 | 0.469 |
| naive: 0.5/0.3/0.2 weighted | 130 | 0.405 |

Per position, ours against the best naive baseline:

| pos | n | ours | 2024 ppg | market |
|---|---|---|---|---|
| QB | 21 | **0.410** | 0.177 | 0.276 |
| RB | 44 | **0.752** | 0.666 | 0.774 |
| WR | 47 | 0.542 | **0.563** | 0.583 |
| TE | 17 | 0.501 | **0.556** | 0.724 |

Two things follow.

1. **The machinery earns its keep at QB and RB, and loses at WR and TE.** At those two
   positions, last season's per-game average — one line of SQL — beats the entire
   pipeline. WR and TE are 64 of the 151 priced players.
2. **The margin over naive is thin overall**: 0.566 against 0.534, i.e. roughly 600
   per-player Sonnet calls (the dominant pipeline cost) buying +0.032 correlation over
   "last year's points per game". Worth knowing before spending more on that path.

Blending ours with the naive baseline peaks at +0.007 (w=0.75) and *lowers* signal
accuracy at 50/50 (58.9% vs 60.3%). Not a free win.

---

## Prerequisite defect: the projections are heavily tied

| pos | n | distinct values | tied players |
|---|---|---|---|
| QB | 23 | 19 | 4 (17%) |
| RB | 53 | 33 | 20 (38%) |
| WR | 55 | 30 | **25 (45%)** |
| TE | 20 | 14 | 6 (30%) |

Board-wide: 752 players hold 179 distinct projected totals. Tied players are
*indistinguishable* to a residual-based signal — the model cannot express a preference it
does not have. Cause is known and documented: the model does prose arithmetic and rounds
("roughly 7 PPR per game across ~16 games" → 112.0).

This is necessary-but-not-sufficient. Breaking ties adds resolution, but resolution is
only worth something if the ordering underneath it is price-orthogonal. Fix it as an
enabler, not as a lever in its own right.

---

## Where orthogonal skill could plausibly come from

Ranked by how specific they are to this system — i.e. how unlikely the market already
prices them. **None of these are measured yet**; that is the point of the next section.

1. **Dependency flags.** The system's stated reason for existing (Keenan Allen signing at
   LAC should cap Ladd McConkey's target share). 513 flags fired on the as-of board across
   32/32 teams. Whether they carry *price-orthogonal* signal is the highest-value open
   question in the whole project and has never been measured.
2. **Beat-reporter signals.** Genuinely late-breaking, plausibly ahead of consensus.
   **The as-of board carried zero of them** — the stage is skipped under an as-of clock
   because live RSS has no archive. So this has never been evaluated even once.
3. **Depth-chart and role changes** inside the as-of window that consensus is slow to
   reprice.
4. **Team-context grades** (`team_metrics`) — scheme, pass rate, O-line. Most at risk of
   being already-in-the-price.

The one existing system output measured to add anything beyond the projection was
`pay_up_flag` (+0.017 R²); `tier` (+0.0008) and `ai_ceiling` (+0.0005) added nothing.

---

## MEASURED 2026-07-25 — the harness is built and the candidates are tested

`measure_orthogonality()` in `backend/engines/backtest.py` (#379) now runs on every
backtest. Results on the as-of board (n=151), obtained by restoring
`backups/rook_asof2025_board_preRestore.sql` into a scratch DB — **no pipeline run, no
cost**:

| candidate | beta | t | sign stability | verdict |
|---|---|---|---|---|
| `dep_displaced` | −0.144 | −1.83 | **98%** | SUGGESTIVE |
| `dep_contingent` | −0.139 | −1.76 | **97%** | SUGGESTIVE |
| `dep_flag_count` | −0.118 | −1.49 | 95% | no signal |
| `tier` | +0.248 | +1.27 | 92% | no signal |
| `injury_risk_modifier` | −0.038 | −0.52 | 71% | no signal |
| `injury_projected_games` | −0.037 | −0.39 | 64% | no signal |
| `dep_net_impact` | −0.007 | −0.09 | 59% | no signal |
| `beat_signal_count` | — | — | — | not enough variation to test |

**The dependency flags are the best candidate found, and the direction is NEGATIVE.**
Flagged players underperform their price even after controlling for price *and*
projection. Raw beat-your-price rates: contingent-flagged **38.2%** vs 52.1% unflagged;
displaced **36.4%** vs 52.5%. Holds within both RB and WR, survives trimming the five
largest residuals, and is negative in 97–98% of bootstrap resamples.

Two things follow:

- `contingent` is labelled `effect_on_value = positive` with average impact **+7.1%**,
  which is the **opposite sign** to what it predicts. Worth fixing on its own.
- `dep_net_impact` — the magnitude the system assigns — carries **nothing** (t = −0.09).
  The flag's *presence* is informative; the system's estimate of *how much* is not.

`injury_*` carrying nothing is consistent with the availability refutation.

`beat_signal_count` is **untested, not useless** — the board holds zero beat signals
because the stage is skipped under an as-of clock. It remains the only completely
unexamined candidate.

---

## What to actually do, in order

1. **Instrument before building.** Add each candidate signal to the backtest as a
   *separate* regressor and measure its partial contribution against `ln(price)`. The test
   is one line conceptually: does it have a non-zero coefficient once price is controlled?
   Anything that fails this cannot help, however good it looks in isolation.
2. **Run 2–3 more as-of seasons — BLOCKED, and not on compute.** Verified 2026-07-25:

   ```
   market_value_historic   2025: 159 priced      (nothing else)
   league_auction_history  EMPTY
   ```

   Only 2025 has real prices. An as-of 2023/2024 board would have nothing to score
   against — `_load_historical_prices` needs ≥50 players and would fall through to
   `market_value_league`, i.e. current-season ADP. That is exactly the contamination that
   voided the 37.3% run. Running those seasons before fixing this would burn $9–72 each
   and produce a number that looks fine and measures nothing.

   **The unblock is a league sync with history, and it needs Stephen.**
   `LeagueSyncService._store_picks` already writes `league_auction_history` per season and
   walks back `settings.league_sync_history_seasons` (default 4) completed seasons, and
   `run_backtest` prefers that table over `market_value_historic`. So a Yahoo sync with
   draft history would populate 2023/2024 automatically — but it requires an OAuth flow.
   Conditional on that league having existed those seasons.

   *Aside, worth guarding:* the 2025 rows are demonstrably a real auction (159 players,
   \$2340 against a 12 × \$200 budget = 97.5% spend, exactly 24 QBs = 12 × 2, 38 players
   at exactly \$1, and **zero non-integer prices** — consensus AAV is fractional). But the
   only code path that writes `market_value_historic` is
   `_snapshot_current_market_values`, which snapshots **FantasyPros consensus**. A future
   real-time run therefore writes consensus into the same table under a different season,
   and the backtest would label it `market_value_historic (YYYY, N=...)` — indistinguishable
   from a real auction in the output. Given this project has already voided one run on
   exactly that class of error, the price source should record how it was obtained.
3. **Fix WR/TE first.** They are where we lose to a one-line baseline and they are 42% of
   the priced board. Understand why before adding anything new.
4. **Break the projection ties** — enabler for everything above.
5. **Only then** spend on new model work, and hold it to the orthogonality test in (1).

### Do not

- Blend in consensus/ADP/expert ranks to raise correlation — **measured zero** signal
  gain at every weight.
- Chase MAE or `within_20pct` as a proxy for signal quality; they are nearly unrelated to
  it here.
- Expand projection spread (refuted — see `signal_accuracy_recon.md`).
- Model availability (refuted — unforecastable among drafted players).

---

## Honest ceiling

Availability is ~51% of the outcome variance and is not forecastable among drafted
players, which caps signal accuracy at **~74.6%**. 70% requires ~0.5 sd of orthogonal
skill and is therefore a genuine stretch, not a tuning exercise. Levers 1–2 moved the
measured basis from 55.6% to 60.3% on one season; whether that holds is itself unverified
until step 2 above is done.
