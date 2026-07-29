# Re-validation of the pooled 62.1% against the current code

Run 2026-07-29. **Result: the pooled signal accuracy is unchanged and still significant.**

The week of 2026-07-26/27 changed the board materially — positional budget enforcement, a
budget gate on `pay_up_flag`, the TE curve fix, and ghost clearing. Every one of those
moves `ai_bid_ceiling`, which `derive_system_signal` reads. The recorded 62.1% therefore
described a system that no longer existed, and needed re-measuring before being quoted
again.

---

## Result

| season | BEFORE (control) | AFTER (current code) | delta |
|---|---|---|---|
| 2023 | 50/76 = 65.8% | 50/73 = **68.5%** | +2.7 |
| 2024 | 52/80 = 65.0% | 48/77 = **62.3%** | −2.7 |
| 2025 | 41/73 = 56.2% | 38/68 = **55.9%** | −0.3 |

```
POOLED BEFORE   143/229 = 62.4%   exact two-sided p = 0.00020   95% CI 55.8-68.7%
POOLED AFTER    136/218 = 62.4%   exact two-sided p = 0.00031   95% CI 55.6-68.8%
```

Identical to a tenth of a point. The per-season moves are ±2.7 in **opposite directions**
and cancel — the same season-variance pattern already recorded in
`code-changes-did-not-move-accuracy`, not a systematic shift. Eleven fewer calls (229 →
218), consistent with the budget gate suppressing `pay_up` on players whose ceiling sits
below market.

Projection metrics are **byte-identical** across the re-run (2023 mae 44.5 / bias 16.7 /
corr 0.775; model accuracy 61.5% on 148, edge_r 0.481). That is the control that proves
only the signal layer moved: none of this week's work touched the projections.

---

## The contamination scare was a false alarm

An earlier note claimed the 2023 component might have been scored against current-season
ADP, which would have invalidated the pooled figure. **It was not.** That reading came
from measuring PRODUCTION, whose price tables are entirely different from the machine the
backtests were run on:

| | dev (where the backtests ran) | prod |
|---|---|---|
| `league_auction_history` 2022 | 174 rows, 174 with `player_id`, $2309 | — |
| 2023 | 175 rows, 175 with `player_id`, $2330 | 160 rows, **0** ids, **$0** |
| 2024 | 177 rows, 177 with `player_id`, $2385 | — |
| 2025 | — | 180 rows, **0** ids, $2367 (league-sync, unusable) |

Both result docs already recorded their price source explicitly — `league_auction_history
(2023, N=175)` and `market_value_historic (2025, N=159)` — which is exactly the safeguard
that settled this. The 2023 and 2025 baselines reproduced their recorded numbers **to the
decimal** on the preserved boards.

The prod data problem is real but separate, and is spun off: the Yahoo league sync writes
auction rows carrying only `yahoo_player_key`, which the backtest cannot resolve.

---

## One discrepancy worth recording

2024's control reproduced as **52/80 = 65.0%**, where the pooled table recorded
**50/78 = 64.1%**. Two calls' difference. The 2024 board dump is from 07-25 and the pooled
figure was computed on 07-26, with commits in between (#396 ceilinged "latest season" at
the clock; #398 re-ranked top_opportunities). The preserved board is therefore very
slightly not the artifact that produced the recorded number.

This does not affect the conclusion: the before/after comparison above uses the **same
board** as its own control, so the delta is clean regardless of how the control compares
to the historical record. 2023 and 2025 reproduced exactly.

---

## Method

Each preserved as-of board was restored into a **throwaway database** rather than over the
dev board — `CREATE DATABASE`, restore, measure, `DROP DATABASE`. No `DROP SCHEMA` on a
real database, and the dev 2026 board was verifiably untouched throughout (4026 players,
728 priced, A.J. Brown → NE). It was dumped first anyway, to
`backups/rook_2026_board_prevalidation.sql`.

For each season:

1. restore `backups/rook_asof<YEAR>_board.sql` into a scratch DB
2. **baseline backtest** on the untouched board — the control
3. `run_valuation_pass` + `write_format_value_sets` → `ValuationAgent.run_all` →
   `enforce_ai_ceiling_budgets` → `reconcile_value_signals`, all under
   `ROOK_ASOF_DATE=<YEAR>-08-15`
4. **backtest again**, with the clock UNSET and the season passed explicitly (under an
   as-of clock the default resolves to the wrong year and finds no prices)

Cost: ~$3 of `valuation_agent` re-reasoning. The expensive stages — the agent phases that
build the profiles — were preserved in the dumps and never re-run, which is what made a
$45 job a $3 one.

---

## What this licenses, and what it does not

The pooled 62.4% on 218 calls at p = 0.0003 is a real, re-verified result on the shipped
system. It is a **pooled, three-season** figure: 2025 standalone remains 55.9% and is not
significant on its own, so the edge is not something to claim for any single season.

Still unresolved and unaffected by this work: prod cannot score its own league's auction
(see the spun-off league-sync issue), so the same validation cannot yet be run against
production data.
