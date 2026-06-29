# Trade Acceptability Model — Design

**Status:** LOCKED. Reviewed with Stephen. Build proceeds from this doc; the five forks below are decided, the §6 open questions are tuning/scope to resolve as noted.

**Purpose:** Replace the current proposal logic — which surfaces only trades where *you* win (a guaranteed-unacceptable robbery, because value today is zero-sum) — with a model that finds **the most favorable trades a rational opposing manager would actually accept.**

---

## 1. The core insight (why the current system can't work)

Today a player's `forward_value` is **intrinsic**: the same number regardless of who rosters him. With intrinsic value, every trade is exactly zero-sum — your `+X` is the opponent's `−X` by construction — so "good for both sides" *cannot exist*, and the current `winner == "you"` bar can only ever surface trades that are bad for the other side. That is the Najee-for-Taylor / LaPorta-robbery class of proposal. It is not a tuning miss; it is structural.

The fix is a second *kind* of value:

> **Contextual value** — what a player is worth *to a specific roster, given what that roster already has.*

Jonathan Taylor is worth a lot to a team starting one weak RB (he becomes their RB1) and little to a team with four startable RBs (he rides their bench). A trade is **positive-sum** precisely when each player is worth more to the roster *receiving* him than to the roster *giving* him up. Finding those is the entire product.

This corrects an early wrong model: contextual value is **not** "his depth-chart slot on the team that holds him." A team that lucked into four startable RBs has a 4th RB who would *start for half the league* — he is surplus to them but a real asset to a needy team. The right question is always: **would this player start for (improve) the receiving roster?** Contextual value is therefore inherently a two-roster computation: `value(player, destination_roster)`.

---

## 2. Primitives

### 2.1 `optimal_lineup(roster) → (starters, lineup_strength)` — shared foundation

Given a roster and the league's lineup rules (`1 QB, 2 RB, 3 WR, 1 TE, 1 FLEX` in the seed), compute the **best legal starting lineup** and its total strength (sum of starters' `forward_value`, FLEX-aware). This is net-new but small, and it is reused by *both* of the pieces below — it is the one genuinely new piece of machinery underneath the whole model.

- "Would player P start for / improve roster R?" = does inserting P raise `optimal_lineup(R).strength`?
- "How much is P worth to R?" = the size of that improvement.
- "How strong is a team?" = `optimal_lineup(R).strength`.

### 2.2 `contextual_value(player, roster)` — the two-roster primitive

A player's value to a roster = his **marginal contribution to that roster's optimal lineup** (displacement-aware):

- If he cracks the optimal lineup, he displaces the current weakest starter at his position (or FLEX); his contextual value ≈ `his forward_value − displaced starter's forward_value` (he is only worth the *upgrade* he provides).
- If he would not start, his value drops toward useful-bench-depth — scaled by how close he is to starting (insurance / upside), bottoming out near replacement level for deep-bench pieces.

Run against the **origin** roster → his value to them (the RB-rich team's surplus back reads low). Run against the **destination** roster → his value to you (reads high if you're thin). The start/sit boundary is **roster-relative** — judged against the receiving roster's actual starters, never a slot count.

**Marginal-curve steepness (Fork 1a — LOCKED):** steep at the start/sit boundary, roster-relative. A startable player is worth roughly his real production-as-upgrade; a genuine sit (the receiver already starts better at the position) is worth much less. Steepness is a **named tunable constant**, calibrated against the real league.

---

## 3. The objective — the edge band

A candidate trade **surfaces only if all four conditions hold**, every value computed in *contextual* terms against the relevant roster:

| # | Condition | Meaning |
|---|-----------|---------|
| 1 | `your_net = your_get_ctx − your_give_ctx > 0` | You improve. |
| 2 | `their_net = their_get_ctx − their_give_ctx > comfort_threshold` | They improve **comfortably** — enough that a rational manager accepts, not a rounding-error gain they'd haggle over. |
| 3 | `your_net > their_net` | You keep the edge — you gain more than they do. |
| 4 | After the trade, `optimal_lineup(you).strength ≥ optimal_lineup(them).strength` | The overtake guard — the trade must not make their team better than yours on the field. |

Conditions 1–3 fall out of the two primitives cheaply. Condition 4 is the whole-roster guard (§4).

**They-gain threshold (Fork 3a — LOCKED):** a **small positive comfort threshold**, not just `> 0`. This is the lever that distinguishes "reasonable trades that actually get accepted" from "technically-positive trades they'd negotiate." Named tunable constant.

**Ranking (Fork 3b — LOCKED):** among surviving candidates, rank by **your edge (`your_net`), subject to their gain being comfortable.** The robbery trades are filtered out by *failing condition 2* (they don't gain), not by capping your edge directly — so the surviving set naturally lands in the sweet spot: you clearly win, they still comfortably gain.

**Real opponent rosters (Fork 2a — LOCKED):** acceptability is scored against each opponent's *actual* roster from `LeagueState`, not a generic manager. This is what makes "can their bench start for you / would they part with this" computable.

---

## 4. The overtake guard (Fork 4 — LOCKED: full, not proxy)

"Don't give them your players if what you give makes their team better than yours still." Implemented as a **full whole-roster comparison**, not the `your_net > their_net` proxy.

- **Measure = starting-lineup strength** (`optimal_lineup(R).strength`): fantasy is won by who you *start*, so "is my team still better than theirs" means "is my best lineup still better than their best lineup after the trade." Condition 4 above enforces this.
- **Bench depth / fragility is a noted refinement (v2):** trading away your only backup at a position makes you more fragile even if this week's lineup is unchanged. v1 measures starting-lineup strength only; bench-depth weighting is deferred.

---

## 5. Integration with the existing system

Both existing endpoints speak intrinsic value today; both move to contextual + acceptability:

- **`/api/trade/ideas` (proposals):** replace `_clears_bar` (`winner == "you"`) with the four-condition edge-band gate (§3); replace the rank-by-`value_delta` with rank-by-`your_net`-subject-to-comfort. The never-fabricate / never-pad rule is **preserved and strengthened** — far fewer candidates clear a four-condition gate than a one-condition one, so "return none with *no clear trade right now*" becomes the common, correct outcome.
- **`/api/trade/analyze` (analyzer):** gains an **acceptability read** on top of the existing verdict. Beyond "is this good for you," it answers "**would they likely accept it?**" — invaluable for a user evaluating a trade *they're* about to send. A trade that's great for you but fails condition 2 gets flagged "they'd probably reject this" rather than presented as a win. (Decision 6c: surface this to the user.)

**Hedging / confidence contract preserved.** A `limited` / `insufficient` / team-change player flows through contextual value carrying its widened uncertainty; the verdict hedges rather than asserting. The **injured-returning-star** case (e.g. Lamar at wk14: low value, `limited` confidence) is the canonical test — the model should read acquiring him as "buy-low, but a bet" (hedged), never a confident steal or a confident pass. This is the trade archetype the whole model most needs to handle gracefully.

---

## 6. Open questions (tuning + scope to resolve during build)

- **6a — Comfort threshold value (§3, cond. 2):** the exact epsilon. Tune against the real 12-team league; ship a sane default, calibrate by eyeballing surfaced proposals.
- **6b — Marginal-curve steepness (§2.2):** the exact start/sit weighting. Same — sane default, tune on real output.
- **6c — Acceptability read in the analyzer: DECIDED — surface it to the user** (headline "they'd likely accept / reject" feature).
- **6d — Candidate enumeration: DECIDED — need/surplus-targeted** (find my surplus, find their need, match), replacing brute-force-plus-filter; cheaper and better given the primitives.
- **6e — Multi-player trades: DECIDED — support bounded 2-for-1 / 2-for-2** (consolidation trades are common and high-value), NOT full combinatorial enumeration. 1-for-1 and bounded multi-player only.

---

## 7. Build sequence (each a PR through `develop`, stop at the develop merge)

1. **`optimal_lineup` primitive** + unit tests (FLEX-aware best-lineup + strength). The shared foundation; nothing else can be built first.
2. **`contextual_value(player, roster)`** + tests, on fixed roster fixtures. Prove the two-roster asymmetry: the RB-rich team's surplus back reads low to them, high to an RB-thin roster.
3. **Roster-strength aggregate + overtake guard** (§4) + tests, reusing `optimal_lineup`.
4. **Edge-band gate** (§3): rewrite proposal surfacing to the four-condition gate + new ranking; strengthen the never-pad tests for the stricter gate.
5. **Analyzer acceptability read** (§5): `/api/trade/analyze` gains "would they accept" (§6c).
6. **Enumeration upgrade** (§6d / §6e): need/surplus-targeted candidate generation; bounded 2-for-1 / 2-for-2 scope.

Each slice validated against the demo 12-team league; the injured-returning-star and the RB-rich-vs-RB-thin pairings are required fixtures throughout.

---

## 8. Deferred ledger (consolidated v2 backlog)

Carried-forward known simplifications, in one place:

- **Bench-depth / roster fragility** in the overtake guard (§4) — v1 is starting-lineup strength only.
- **Cross-team target-share normalization** in the value engine (from the team-change handling, #151) — v1 flags + widens uncertainty, doesn't normalize.
- **Positional-scarcity-aware drop recommendations** in the analyzer (slice 3) — v1 drops by raw `forward_value`.
- **Bye-week context** in acceptability — `bye_week` is currently unpopulated; wire only if the model ends up wanting it.
- **Value-blend calibration** (`_RECENT_VS_SEASON_WEIGHT = 0.5`, #158) — provisional, calibrate against the real league.

**Resolved / superseded:**
- "Mutual-benefit vs user-win framing" (flagged at slice 4) — *superseded by this entire model*; the edge band is the resolution.
- "Usage-aware blend for returning players" — investigated and rejected: the injured-returning-star (Lamar) is correct behavior at the demo's week-14 anchor, not a bug. No fix needed.
