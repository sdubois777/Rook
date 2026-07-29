# Do the `REPLACEMENT_LEVEL_PPR_PER_GAME` floors still earn their keep?

Answer: **no.** They were never calibrated, they shared no basis with each other, and
because they were also applied format-blind they were distorting the Half and Standard
boards harder than the PPR one the question was raised about.

Task from `docs/recon/replacement_floor_handoff.md` §2. Branch
`fix/replacement-floor-common-basis`, cut from `develop` at `b4c8388` (tree-identical to
`main` at `8f4be58`).

---

## 1. The question, and the answer

The handoff framed it narrowly and mechanically:

> Is the dynamic replacement value at RB (and QB) *bad data* — the thing the floor exists
> to catch — or is it *real*? If it is real, the floor is overriding a correct measurement.

**It is real.** Three independent checks, none of which uses the market:

**a. Neither failure mode the docstring names is present.** The docstring says a low
dynamic value means "too few profiles, skewed sample".

- *Too few profiles* — every pool is full, with room to spare. RB has 152 valued players
  for a 52-deep pool; WR 265 for 60. `calculate_replacement_level`'s short-pool fallback
  is never reached.
- *Skewed sample* — the projections step smoothly through the replacement slot. In the
  21 ranks around it: RB has 16 distinct values (largest tie 3), QB 15 (largest tie 3).
  No plateau, no cliff. Ironically the two positions that *are* visibly bucketed are WR
  (8 distinct values in 21 rows, a 7-way tie at 130.8) and TE (5-way tie at 148.0) —
  and those are the two where the floor does not bind.

**b. History says the dynamic value is generous, not depressed.** At each position's own
draftable-pool rank, over 2023-25:

| pos | rank | ex-post season total | as PPG | rank-N by PPG (≥8 gp) | old floor PPG |
|---|---|---|---|---|---|
| QB | 13 | 254.7 | 14.98 | 17.03 | 17.0 |
| RB | 52 | **86.8** | **5.11** | **6.27** | **8.0** |
| WR | 60 | 113.1 | 6.65 | 8.33 | 7.0 |
| TE | 18 | 124.3 | 7.31 | 9.03 | 5.0 |

Ex-post rank is the *generous* direction — the season-end 52nd-best RB is a maximum over
every candidate who could have held that slot, so it overstates what a drafter could
expect from whoever they actually took at RB52. The board's own RB52 projection (6.9 PPG
on prod, 6.6 on dev) already sits **above** both historical bases. The floor's 8.0 sits
above everything. It is not catching a depressed measurement; it is inflating a healthy
one.

**c. The floor contradicts the pool size — the same fact stated twice, incompatibly.**
`get_draftable_pool_sizes` says 52 RBs get drafted. A floor of 136 season points lands at
roughly RB44 on the board. So the system simultaneously asserted "you will draft these 52
RBs" and "8 of them are worth less than a free player" — those 8 had their PAR clipped to
zero *inside their own draftable pool*. At QB it was 3 of 13, including Burrow and
Stafford.

The knock-on: with RB replacement lifted 18 points, the top of the RB curve was pushed
into `MAX_REALISTIC_BID`. Gibbs' anchor computed to $80.0 — exactly the cap — so a second
rail began binding purely as a consequence of the first.

---

## 2. The bigger finding: the floors were format-blind

Not in the original scope, and worth stating plainly because it changes the severity.

`REPLACEMENT_LEVEL_PPR_PER_GAME` was a PPR-shaped constant, but `write_format_value_sets`
applied it to Half and Standard points too. Standard strips a full point per reception, so
the identical season-points bar is a far higher hurdle there. Measured on the live board:

```
format     pos    dynamic   old floor   binds?
ppr        QB       285.0     289.0     BINDS +4
ppr        RB       118.0     136.0     BINDS +18
half_ppr   QB       285.0     289.0     BINDS +4
half_ppr   RB       109.0     136.0     BINDS +27
standard   QB       285.0     289.0     BINDS +4
standard   RB       100.0     136.0     BINDS +36     <- replacement lifted 36%
standard   WR       103.0     119.0     BINDS +16
```

**Five** binding format×position pairs, not the two the PPR-only view showed — and the
worst distortion was on the Standard board, which the constants were never calibrated for
at all. The PPR-only framing in the handoff understated the problem.

This also caught a mistake of mine mid-task: my first pass recalibrated the floors on PPR
actuals only, which pushed TE's floor to 7.2 PPG and made it bind on Standard TE,
collapsing that position's budget share from 0.111 to 0.006. The per-format calibration
below is what fixes it, and `test_replacement_floors_never_bind_on_a_healthy_board` now
checks all twelve pairs so the same class of error cannot ship silently.

---

## 3. What changed

`REPLACEMENT_LEVEL_PPR_PER_GAME` → **`REPLACEMENT_FLOOR_PPG`**, now keyed by format, read
through a `replacement_floor(position, scoring_format)` accessor. The rename is deliberate:
a constant with "PPR" in its name holding non-PPR rows is exactly the misreading that
caused the format-blind bug. Only two call sites existed, so forcing both through the
accessor is cheap insurance.

**One rule, twelve values, no free parameter.** The floor is the worst the position's own
pool rank has delivered in the three completed seasons `get_analysis_seasons(3)` returns —
taken as the minimum across four slot definitions (rank by season total; the same with a
≥8-game gate; rank by PPG at ≥8 and at ≥4 games, scaled to 17). History is repriced
through `backend.scoring.season_points`, the same function the board reprices with, so the
guard and the thing it guards share one basis.

```python
REPLACEMENT_FLOOR_PPG = {
    "ppr":      {"QB": 14.1, "RB": 4.8, "WR": 5.9, "TE": 7.2},
    "half_ppr": {"QB": 14.1, "RB": 4.1, "WR": 5.0, "TE": 5.7},
    "standard": {"QB": 14.1, "RB": 3.6, "WR": 3.8, "TE": 4.2},
}
```

QB is identical in all three by construction (no receptions). Every one of the twelve is
now inert on the live board, with +22 to +45 points of headroom — which is what a bad-data
guard should look like.

Two supporting changes:

- **`calculate_replacement_level` now warns loudly on a short pool.** That is the *real*
  "too few profiles" failure: replacement gets measured at the wrong rank and silently
  sets the steepness of the whole position's curve. It used to pass in silence, and the
  magnitude floor could only catch it by accident. A guard you can see fire beats one that
  quietly rewrites the board.
- **A binding floor now logs at WARNING, not INFO.** These are calibrated never to bind;
  if one does, the projections are the thing to fix.

Also removed the stale `REPLACEMENT_LEVEL_PPR` block from `docs/rules/LEAGUE_RULES.md`,
which still read `QB: 18.0` long after the code moved to 17.0, and whose "waiver wire RB"
gloss described a *freely available* player — which by definition must sit below the last
drafted one, not above it. The doc now points at the code and says replacement is measured.

---

## 4. Verification

Re-valued the dev board with `run_valuation_pass` + `write_format_value_sets` →
`ValuationAgent.run_all` → `enforce_ai_ceiling_budgets` → `reconcile_value_signals`, per
§3 of the handoff. No pipeline CLI, so no `sync_rosters` / `sync_adp` confound.

**Allocation harness — the required invariant holds.** Draftable-pool basis, all three
formats:

| format | pool spend | target | worst positional deviation |
|---|---|---|---|
| ppr | $2220 | $2220 | **0.0** |
| half_ppr | $2220 | $2220 | **0.0** |
| standard | $2220 | $2220 | **0.0** |

Worth being precise about what this proves: `enforce_ai_ceiling_budgets` renormalises each
position's top-N onto `pool × share`, so 0.0/$2220 is true *by construction* after
enforcement. It is a regression guard that enforcement still runs and still targets the
right population — not evidence the replacement change was good. The whole-table rows did
improve on their own terms (Standard's worst deviation 10.7 → 2.8, Half's 5.4 → 3.2,
because the budget shares are no longer computed off a distorted Standard PAR), and the
untouched depth tail is byte-identical at $568 over 535 rows.

**Curve harness — the distortion was at PAR, and it is gone.** top1/top5 as a share of the
position's pool, at each stage:

```
pos          points              PAR                anchor $            ai $         MARKET
QB       9.2 / 40.9    33.1/77.8 -> 29.7/73.8   28.3/75.1 -> 27.4/71.9   24.5 -> 25.0   19.9/57.4
RB       4.0 / 15.6     9.6/31.2 ->  7.4/25.1    9.3/30.7 ->  7.4/25.0    8.8 ->  7.1    6.2/26.6
WR       2.9 / 13.3     6.6/28.9 ->  6.6/28.9         unchanged           6.1 ->  6.2    6.1/25.6
TE       7.1 / 32.3    16.4/60.0 -> 16.4/60.0         unchanged          14.8 -> 14.8   17.8/56.9
```

The `points` column is identical before and after — same profiles, same projections. Every
difference is the replacement transform, introduced at PAR and carried downstream, exactly
as the harness is built to show. WR and TE do not move at all, because their floors never
bound. The `used` column of the clamp table now reads `dynamic` at all four positions.

**Dollars.** RB is where it lands:

| player | anchor before → after | ai$ before → after | market |
|---|---|---|---|
| Jahmyr Gibbs | 80.0 → 63.2 | 75 → 61 | $56 |
| Bijan Robinson | 64.1 → 50.4 | 60 → 51 | $55 |
| Kyren Williams | 43.6 → 36.0 | 41 → 33 | $26 |

$1 players *inside* the draftable pool: RB **12 → 3**, QB 3 → 3, WR/TE unchanged. (The
three remaining at each of QB and RB are the players at or below the dynamic replacement
itself, whose surplus is zero by definition — correct, not a residue.)

QB moved by ±$3 at most; its floor was only 1.4% above the dynamic value on dev.

**Not market-fitting — the change crosses the market rather than converging on it.** RB's
PAR top-5 concentration goes 31.2% → 25.1% against a market of 26.6%: it was above, it is
now slightly below. The market was used only to notice the artifact, per §4 of the handoff;
the evidence is the historical actuals and the internal contradiction with the pool size.
The `±5/−8/−15` backtest cuts were not touched.

**Tests:** 2491 passing, 2 skipped, 1 failing — `test_give_side_diversity.py::test_ben_dover_no_longer
_ships_allen_in_every_trade`, the pre-existing dev-DB-seeded failure the handoff documents
as expected. Six new tests cover the format-monotonicity of the floors, the unknown-format
and unknown-position fallbacks, the twelve-pair no-bind invariant, floor-below-cap, and the
short-pool warning firing and not crying wolf.

---

## 5. What this does NOT claim

- **No accuracy claim.** Nothing here shows the change improves signal accuracy, and the
  usual route to checking is closed: `backtest.py` thresholds on the *dollar* gap, and the
  board rescaled on 07-26, so numbers either side of that date are not comparable. The
  argument is correctness — a constant was asserting something three seasons of actuals
  contradict — not measured edge.
- **Prod was not read.** Every measurement here is the dev board; the permission layer in
  this session blocked `ROOK_ENV_FILE=.env.prod` even for read-only queries. The prod
  numbers quoted are the handoff's own. This matters: dev and prod differ (WR's dynamic
  replacement is 142.0 on dev vs 118.3 on prod), and WR's old floor bound on prod but not
  on dev. **The conclusion is unaffected** — RB's floor bound by +15.3% on prod and +23.5%
  on dev, and history contradicts 8.0 PPG on either — but the exact post-change prod
  numbers should be re-measured before release.
- **Per-format `ai_bid_ceiling` was not regenerated.** `write_format_value_sets` writes
  anchors, not ceilings; those come from `run_prose_for_format`, which is a separate paid
  stage and is not in the handoff's re-valuation recipe. The Half/Standard `ai$` columns
  are therefore the previous agent's numbers railed once against the new anchors. The
  format *anchors* and *replacement levels* are fully regenerated and are the meaningful
  evidence there.

---

## 6. Left alone, deliberately

- **`REPLACEMENT_LEVEL_MAX_PPR_PER_GAME` has the identical format-blind defect.** It is
  inert in all three formats on the current board (checked — nothing within 50 points of
  binding), and TE's cap was deliberately tuned to 9.0 in the recent TE work. Changing it
  would disturb shipped, validated work with no evidence of a problem. Flagged in a comment
  on the constant.
- **The RB pool depth (`_BENCH_SPLIT["RB"] = 0.28`, 52 RBs) was not touched.** There is a
  real football argument that RB replacement should sit above the naive pool-last player,
  because an injury replacement plays like RB30, not RB52. If that belief is worth
  encoding, the honest lever is the pool size — which is where the TE fix correctly went —
  not a clamp that silently contradicts it. That is a judgment call about roster dynamics
  and belongs to you, not to a bad-data guard.
- **~54 duplicate player rows are visible at the RB/QB replacement slot.** The dev board
  has "Le'Veon Bell" (RB, TB, age 29) at rank 44 and "Ben Roethlisberger" (QB, PIT, age 39)
  at QB19, both carrying projections. These are the name-key collisions CLAUDE.md rule 7
  warns about. They inflate the dynamic replacement slightly (junk at rank 44 pushes a real
  player down to rank 52), so cleaning them would move replacement *further below* the old
  floor — it strengthens this conclusion rather than threatening it. Not fixed here.
