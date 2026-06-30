# Trade Value — Lineup-Improvement Objective: Design

**Status:** LOCKED. Reviewed with Stephen. Core fix (resulting-roster, points/week, two-clause rule, ascending-usage depth) builds now; handcuff-upside depth is the NEXT slice (§5b).

**Purpose:** Make trade evaluation measure **whether the trade makes your team better** — the improvement to your starting lineup, in real points per week — instead of the **sum of the values of the players involved.**

## 1. The bug (recon-confirmed)
The edge band rewards value *accumulation*, never *lineup improvement*. Four stacked defects, same root — summing player values instead of evaluating the resulting roster:
1. **Independent per-player sum double-counts.** `your_get_ctx = Σ contextual_value(x, my_roster)` values each incoming player independently against the original roster; three players competing for one slot each get credit. Proof trade: real lineup gain +9.4 (only Brian Thomas starts; Odunze + Helm ride the bench), but `your_net` reads +23.2.
2. **"Improve my starting lineup" was specified, never wired** (`trade_agent_design.md:175`; acceptability design). Only `your_net > 0` proxies it, conflating lineup gain with bench-depth accumulation.
3. **Forced drops aren't debited.** Proof trade nets +2 → forces dropping Bateman (4.6) + Njoku (6.5); neither `your_net` nor the verdict subtracts them. Acquiring bench bodies by cutting real players reads as pure gain.
4. **The verdict is raw Σforward_value.** "+35.8 lopsided you win" = `Σforward_value(get) − Σforward_value(give)` — ignores lineup, ignores drops, in a currency no human reads.

## 2. The principle (fixes all four at once)
> A trade is evaluated by the change in your STARTING LINEUP's projected points, computed on the RESULTING roster (after all incoming, outgoing, and forced-drop moves), evaluated ONCE — not by the sum of the values of the players involved.

Kills the double-count (evaluate the resulting lineup once); debits forced drops automatically (they're gone from the resulting roster); makes the lineup gate natural; fixes the verdict; and catches the same bug on the opponent's side (`their_net` is summed the same broken way).

## 3. Currency — points per week (DECIDED)
Internal player ranking stays 0-100 `forward_value`. The verdict + lineup gate are in real projected points/week, because 0-100 deltas are meaningless and non-additive, and points/week is the honest unit that exposes the bug. **Conversion:** `lineup_strength_ppg(roster) = Σ forward_ppg of optimal starters` (the points-based level the pipeline computes before scaling to 0-100). Verdict reads "improves your starting lineup by +X.X points/week."

## 4. The value rule (DECIDED — replaces `your_net > 0`)
A trade has value to you only if it either:
- **(a)** improves your starting lineup by **≥ _LINEUP_GAIN_THRESHOLD** (DECIDED 5-8 ppg; ship anchored at the LOW end ~5, tune up — below ~5 is noise dwarfed by weekly variance), OR
- **(b)** *maintains* your starting lineup (Δlineup ≥ ~0, small tolerance — the "maintains" anti-churn guard) AND meaningfully improves your bench/depth.
Fail both → no value to you → does not surface. Both clauses computed on the RESULTING roster, so forced drops are already debited (can't satisfy (b) by cannibalizing your team).

## 5. Depth handling (DECIDED — heavily discounted, never standalone)
Bench/depth value is heavily discounted vs starting value and can NEVER be the sole surfacing reason — only clause (b), which requires the lineup maintained.

### 5a. Clause (b) v1 — ascending-usage only (THIS slice)
"Meaningfully improves your bench" = the incoming bench player has a **rising-usage / buy-low signal** (the #170 trajectory/opp-gap signals, already computed) — a rising star ascending toward a starting role. A static replacement-level bench piece with flat usage (Odunze/Helm in the proof trade) has NO depth value → clause (b) won't fire → bench-churn still can't surface. Ship strict.

### 5b. Handcuff-upside depth — NEXT SLICE (ledgered, design baked in)
A backup behind a workhorse has contingent value (one injury from a workload). The link EXISTS in Postgres: `player_dependencies` (Roster Changes agent, pre-draft pipeline) stores `trigger_player_id` + `trigger_condition` (injured/absent) + contingent/beneficiary flag — explicit RB1→RB2, no new source.
**Design (for the next slice):** `player_dependencies` is **preseason-projected** — a structural PRIOR, NOT live truth. It must be **validated against live #149 usage**: a preseason handcuff still behind a still-high-usage workhorse → real insurance value; a handcuff whose workhorse-ahead has seen usage collapse (committee resolved / role shrank) → stale, discount/drop. Wire at the **league-state assembly layer** (already has canonical_player_id + nfl_team, already hits Postgres for the real provider); thread role/handcuff context into the value computation as a new input. Combine structure (who's behind whom) + current usage (is it still true); never trust the preseason flag as live.

## 6. The revised gate (all in points/week, both sides evaluate resulting-roster)
1. **Value to you (NEW §4 rule):** clears clause (a) OR (b).
2. **Acceptable to them:** the SAME §4 rule on the opponent's resulting roster (improves their lineup, or maintains-and-improves their depth). Replaces bench-conflated `their_net`.
3. **You keep the edge:** your lineup gain > their lineup gain (in lineup-ppg).
4. **No overtake:** the #168 relative guard, on resulting-lineup strength.

## 7. The verdict (DECIDED)
Headline: **"Improves your starting lineup by +X.X points/week"** (real Δlineup ppg, net of drops) — not raw value-delta. Acceptability ("they'd likely accept/reject") = does it improve their lineup. Per-player grounding stays. A below-threshold analyzer trade reads honestly ("barely moves your lineup, +1.3 ppg — not worth it"), never "lopsided win."

## 8. Acceptance tests
1. **PROOF TRADE READS HONESTLY (headline):** Rachaad White → Brian Thomas + Odunze + Helm (forces dropping Bateman + Njoku) → NOT "+35.8 lopsided win"; reads the real small lineup gain net of drops (marginal/negative) and does NOT surface (fails §4). Assert it no longer clears.
2. **DOUBLE-COUNT KILLED:** a 1-for-3 where only one incoming starts → valued at the ONE-slot lineup gain, not three players' sum.
3. **FORCED DROPS DEBITED:** a +players trade forcing real-asset drops → dropped lineup contribution subtracted; downgrading bench to add scrubs reads as a loss.
4. **GENUINE UPGRADE SURFACES:** a real 5+ ppg starter upgrade clears clause (a).
5. **DEPTH PATH GUARDED (ascending-usage):** a lineup-neutral trade acquiring a rising-usage/buy-low bench player (starters untouched, no real cut) passes clause (b); a flat-usage bench-churn trade that downgrades the lineup fails it.
6. **OPPONENT SIDE FIXED:** acceptability uses the opponent's resulting-lineup improvement, not summed `their_net`.
7. **VERDICT IN POINTS/WEEK:** headline is "+X.X points/week," not a 0-100 delta.

## 9. Decisions + open
- Currency points/week; threshold 5-8 ppg (anchor low ~5, tune up); two-clause rule with maintains-guard; depth heavily discounted, never standalone — all DECIDED.
- Clause (b) v1 = ascending-usage only (this slice); handcuff = next slice (§5b).
- Calibration reset: re-rates every trade; re-eyeball after. `_COMFORT_THRESHOLD` largely SUPERSEDED — the comfort bar becomes "improves their lineup," not a value epsilon.

## 10. Why this is (again) late
Second founding behavior found specified-but-never-wired (first was the trajectory signal). The acceptability design said "would P improve roster R = does it raise optimal_lineup strength"; the machinery was built but wired as a per-player sum against the original roster — value accumulation, not lineup improvement. This makes the code match the design's intent.
