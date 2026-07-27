# Build report — positional budget enforcement

Implements `docs/recon/positional_budget_enforcement_handoff.md`. Branch
`feature/positional-budget-enforcement`, off `develop` (= `main` = `d4d055b`).

Every number below is measured on the local dev board, not inferred.

---

## Result

| | baseline | after | §5 requires |
|---|---|---|---|
| worst positional deviation (PPR) | 10.1 points | **0.0 points** | ≤ 2 |
| pool spend (PPR) | $3556 vs $2220 (+60.2%) | **$2220 (+0.0%)** | within 2% |
| worst deviation, half-PPR | 11.7 points | **0.0 points** | — |
| worst deviation, Standard | 11.7 points | **0.0 points** | — |
| values below $1 | 0 | **0** | none |
| values above `MAX_REALISTIC_BID` | 0 | **0** | none |
| PAY UP players showing a negative gap | 6 of 13 | **0 of 14** | — |

The board was re-measured **after** re-running `valuation_agent`, which is the point:
the agent rewrites `ai_bid_ceiling`, so enforcement that does not survive it has not
worked. 96 calls, $1.00, 728 players processed, 0 skipped.

QB lands on exactly $184 in all three scoring formats — format-invariance now *holds*
as a consequence of the restructure rather than being the reason QB escaped the rail.

---

## What was wrong

Three layers, and the sum-to-budget constraint was missing from all of them for QB:

```
run_valuation_pass   -> recommended_bid_ceiling   pool-share; algebra sound
valuation_agent      -> ai_bid_ceiling            LLM opinion, ±25% off the anchor
_apply_hybrid_rails  -> sum-to-budget rescale     RB/WR/TE ONLY -- QB unrailed
```

QB's exclusion was inherited from `_FORMAT_INVARIANT_POSITIONS` / `_TIER_BAND_POSITIONS`.
Those sets exist because QB *points* are identical in every scoring format — QBs don't
catch passes. That is real and is preserved. Budget enforcement is not format math and
should never have inherited the exclusion.

The shape of the bug, from the pre-fix board: **C.J. Stroud and Matthew Stafford were
written at $32 against a $1 math anchor** — comfortably under the $50 QB cap, so the only
guard in the path never fired.

Separately, `POSITION_BUDGET_SHARE` summed to **0.90**, stranding $222 of the $2220 pool
in dollars no position could ever be allocated.

---

## What changed

1. **`enforce_position_budget()`** (`backend/engines/valuation.py`) — one pure
   water-filling function, applied to **every** position. Scale the unclamped values onto
   the target, pin whatever hits `[1, MAX_REALISTIC_BID[pos]]`, redistribute the residual
   over what is still free, repeat (N=20). A single rescale cannot work here: it fights
   the clamps and the clamped dollars silently vanish or appear.

   `apply_board_budgets()` wraps it for a whole board. Both are pure and
   fixture-injectable — the tests need no DB.

2. **`enforce_ai_ceiling_budgets()` runs AFTER `valuation_agent`**, not inside
   `run_valuation_pass`. This was the trap called out in §6 and it is load-bearing:
   `run_valuation_pass` writes `recommended_bid_ceiling`, the agent then overwrites
   `ai_bid_ceiling`, and only the second reaches the board. Wired into
   `scripts/run_predraft_pipeline.py` between the agent and `reconcile_value_signals`
   (so `value_gap` is computed against the final ceiling) and before
   `run_prose_for_format` (which copies QB/K/DEF out of the players table).
   `enforce_format_ai_ceiling_budgets()` does the same for the per-format rows after the
   hybrid writes them.

   Also wired into `run_targeted_refresh`: the budget is board-wide, so re-reasoning even
   one ceiling breaks it for every position. Enforcement is pure Python and free, so it
   runs on every path that writes `ai_bid_ceiling`.

3. **`_apply_hybrid_rails` is now per-position and covers every position.** Rail (2)
   rescales to the anchor aggregate and rail (3) caps each tier at the better tier's max;
   both are only meaningful *inside* one position. Over the merged RB+WR+TE pool one
   position absorbed another's slack, and the tier-ordinal compared a tier-1 QB to a
   tier-2 RB.

4. **`POSITION_BUDGET_SHARE` renormalised to 1.0** and set from this league's own auction
   history (three completed seasons from `league_auction_history`, recency-weighted
   1/2/3): QB 0.083 / RB 0.385 / WR 0.456 / TE 0.076. Non-PPR needs no new numbers —
   `_format_budget_shares` anchors on this dict, so Standard correctly moves budget to RB
   (measured: RB 0.496 / WR 0.360 / TE 0.061).

5. **The GAP column reads the server's `value_gap`** (`getValueGap` in
   `frontend/src/utils/playerUtils.js`). `DraftBoard.jsx` was recomputing
   `ai_bid_ceiling − market_value` — the legacy dollar gap #378 retired for being
   price-contaminated — in a column adjacent to a PAY UP badge derived from conviction.
   The TXT cheat-sheet export had the same bug and `TeamDetail.jsx` read the raw field.
   `value_gap` is now in the source-scan guard in `playerUtils.test.jsx`, so a component
   cannot reintroduce it.

---

## Which players the budget is measured over

**The draftable pool** (`get_draftable_pool_sizes` — QB 13 / RB 52 / WR 60 / TE 25, the
same top-N the replacement level and the z-tiers are already built on), not every priced
row. This is a deliberate deviation from a literal reading of §5, and it is load-bearing.

A 12-team league buys ~150 skill players. The board prices 673. Charging the $1 depth
tail against the pool is a category error — nobody buys those players, so they never
spend league money — and it is arithmetically degenerate. Holding every priced row to
`share × pool` with a $1 floor gives TE 146 rows against a 7.6% share: $146 of that
$169 is floor, leaving **$23 to split among all of them and pricing the best TE in
football at $3**. QB is the same shape ($89 across 30 above-floor QBs → a $12 Josh
Allen). Those two criteria — per-position shares within 2 points AND total within 2% over
every priced row with a $1 floor — are jointly satisfiable *only* by that degenerate
board.

The tail rides the same scale factor and floors at $1, so ordering is continuous across
the pool boundary and no visible player is dropped. Reported on both bases:

```
DRAFTABLE POOL basis (budgeted):   $2220 / $2220  (+0.0%),  worst 0.0 points
ALL PRICED ROWS basis:             $2743 / $2220 (+23.6%),  worst 3.6 points
                                   ...the difference is $523 of $1 depth over 523 rows
```

Baseline on the all-rows basis was $3556 (+60.2%) and worst 10.1, so that basis improves
too — it just cannot reach 2% without the degenerate board.

---

## Tests

- `tests/unit/engines/test_position_budget.py` (20) — `test_every_position_lands_on_its_budget`
  parameterised over all three formats over a hostile synthetic board; ordering
  preservation; the QB regression itself; the water-filling primitive (cap residual
  redistribution, floor, infeasible targets, budgeted subset); and the async DB pass
  writing `ai_bid_ceiling` rather than the math anchor.
- `tests/unit/agents/test_valuation_agent_rails.py` (8) — per-position scoping, QB railed
  to its anchor rather than merely capped, no cross-position tier-ordinal comparison.
- `tests/unit/test_config_env_layering.py` (9) — §7, committed first and separately.
- `frontend/src/test/playerUtils.test.jsx` — gap sign never contradicts PAY UP, on the
  four measured prod rows.

Full suite green apart from the one failure §5 documents as expected on clean `develop`
(`test_give_side_diversity.py::test_ben_dover_no_longer_ships_allen_in_every_trade`).

---

## Two pre-existing bugs found and fixed in passing

- **`backend/config.py` env layering** (§7) — committed first, on its own, with tests.
  `.env.prod` is a DATABASE_URL-only overlay; pydantic-settings *replaces* the env file
  when handed a single path, so `ROOK_ENV_FILE=.env.prod` died at import on the required
  `anthropic_api_key` / `secret_key`. `resolve_env_files()` layers `(base, selected)`.
  The prod-write guard is untouched — it keys on the resolved DB host, which is exactly
  what the overlay changes.
- **`tests/unit/engines/test_valuation.py:525`** — `Path.read_text()` with no encoding.
  On a Windows dev box that is cp1252, and `valuation.py` has always held non-cp1252 bytes
  (the box-drawing banner in the TIERING section), so the no-hardcoded-years guard raised
  `UnicodeDecodeError` instead of running. Green in CI (UTF-8 locale), broken locally.
  Same latent bug in six other guard tests — spun off, not widened into this diff.

---

## Open items

**The within-TE curve is flat and it is now visible.** Our top TE prices at $16 against a
$31 market. The positional total is not the problem — TE's 7.6% is this league's own
measured spend and enforcement hits it exactly. The distribution is:

```
pos    ours top1 / top5     market top1 / top5     our #1 vs market #1
QB      24.5% / 72.8%        19.9% / 57.4%          $45 vs $27
RB       9.0% / 30.2%         6.2% / 26.6%          $77 vs $58
WR       6.1% / 26.9%         6.0% / 25.0%          $62 vs $66
TE       9.5% / 35.5%        17.7% / 56.6%          $16 vs $31
```

QB/RB/WR are at or steeper than the market. TE is flat by ~2x. Enforcement is a monotone
rescale, so it did not create this — it made it visible by halving TE's total. This is a
within-position (conviction) question, deliberately out of scope here. Spun off.

**The backtest still thresholds on the retired dollar gap.** `backend/engines/backtest.py:946`
recomputes `value_gap = ai_bid_ceiling − price` and feeds it to `derive_system_signal`.
The *direction* of every call is safe — `pay_up_flag` wins first and `value_assessment`
gates rules 3 and 4, and both are conviction-derived, so signal accuracy's basis is
untouched exactly as §6 says. But the magnitude cuts (+5 / −8 / −15) are on the raw
dollar gap, and a rescaled board makes that gap systematically more negative, which will
change how many `avoid` calls the backtest makes. Any movement in the 62.1% would be a
measurement artifact of backtest.py not having followed #378, not a change in edge.
Spun off; belongs with the measurement-integrity work.

**Not verified in prod.** Everything above is the local dev board. Prod was untouched and
read-only throughout. Backend changes do nothing in prod until released, and Stephen
drives every release.
