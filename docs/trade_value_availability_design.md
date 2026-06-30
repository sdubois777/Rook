# Trade Value — Player Availability (Staleness Decay + Bye Handling): Design

**Status:** LOCKED. Reviewed with Stephen. Decisions 8b (conservative decay) and 8c (free-weeks = 1) are made. This REOPENS the deliberately-frozen value engine (recency / season / usage-trend / confidence), so it carries the same discipline as #158/#170/#172 — explicit reconciliation + a calibration pass.

**Purpose:** Stop injured / season-ended players from retaining stale pre-injury value (Tucker Kraft: out since week 9, reads forward_value 70.9 and "sell-high" at week 14). Simultaneously handle BYES correctly — a bye is not a zero and not staleness; a player around a bye should read his real recent form, not be penalized.

---

## 1. The bug (recon-confirmed)

The value engine's window operates over **games played**, with no notion of *when* they were played. `_played_weeks` keeps rows that exist (byes/inactives produce no row); `recency_ppg` is the last 3 *rows*; `season_ppg` averages over *games played* (denominator = games played, not weeks elapsed); `usage_trend` is last-2 vs prior-3 *played* rows. So **a player who stops playing keeps his pre-injury value indefinitely, with zero decay** — Kraft has 8 played rows ending week 9, full confidence (games ≥ 4), and reads 70.9 off month-stale weeks. The verdict even narrates a "fading role / regression" as if he's active.

**Blast radius (schedule-confirmed injuries, byes excluded):** ~10 meaningfully-inflated absentees — Kraft (70.9, out 5w), Drake London (94.9, 3w), Skattebo (60.4), Tyreek (50.7, 10w), LaPorta (47.9), Travis Hunter (47.1, reads BUY-LOW), Dobbins, Conner, Nabers, G. Wilson. Two downstream harms: (a) **phantom trades** — the engine surfaces acquiring/dealing dead players (Big Black Cop deals Kraft; Joe Shiesty acquires Kraft *paying with* Travis Hunter — a trade of two injured players); (b) **lineup inflation masks real upgrade paths** — London's phantom 94.9 makes The Lord's WR lineup look so strong no real WR clears cond1, suppressing ~3 teams' trades.

---

## 2. The two mechanics (they are SEPARATE — this is the design crux)

The recon's key finding: **skipping byes alone does NOT fix the bug.** A naïve "skip byes, reach back to fill 3 played weeks" pulls Kraft's weeks 7-9 — his pre-injury form — cleanly serving up stale data. The fix needs **two distinct mechanics**, and conflating them is the trap:

### 2a. Bye handling (window mechanic) — Stephen's insight
A bye is *scheduled unavailability*, not performance and not staleness. So:
- A missed week that coincides with the player's team's bye is **skipped, not counted as a zero, and does not count toward staleness**. The window reaches back one more *played* week to stay full.
- Effect: "22, [bye], 20" reads as strong recent form (22 and 20 over his last two games), not diluted. This *raises* accuracy for every player who's had a bye, healthy ones included — the current skip-everything logic mishandles this silently; we only noticed via Kraft's absurd output.
- Data: the NFL **schedule is already pulled and cached** (`fetch_schedules` → `warehouse.schedule`; `_get_bye_week` helper exists; `player_schedules.bye_week` column exists). Use it directly — NO new source (handcuff lesson). The seed's `bye_week=None` means "not populated on the demo RosterPlayer," not "unavailable."

### 2b. Staleness decay (the actual inflation fix) — the genuinely new piece
Independent of byes: a player's value must **decay toward the floor as his most recent *played* week recedes**, where "recede" counts elapsed weeks **excluding byes**.
- `weeks_stale = current_week − last_played_week − (byes in that span)`.
- `weeks_stale = 0–1`: no decay (current, or a single non-bye gap — a one-game absence shouldn't crater a stud).
- `weeks_stale` growing: decay the **base `inseason_level` / forward_value toward the replacement floor**, scaled by how stale, bounded.
- Kraft (5 weeks stale, no byes in span) → heavily decayed → near floor → no longer tradeable, no longer surfaces as a target.
- This is the piece that actually fixes the inflation. The bye-skip just makes the *window* honest; the decay makes the *value* honest.

---

## 3. The composition path (reopen the frozen engine safely)

Staleness feeds TWO places. One is free; one is the new work.

### 3a. Staleness → confidence (FREE — reuses #170 machinery)
`_assess_confidence` is currently games-count only (`<2 INSUFFICIENT, <4 LIMITED, else FULL`) — Kraft gets FULL off 8 stale games, the confidence half of the bug. **Feed `weeks_stale` into confidence**: a stale player downgrades (FULL→LIMITED→INSUFFICIENT as staleness grows). Because the #170 trajectory + opp-gap factors are *already* confidence-scaled (`_value_confidence_scale`: FULL 1.0, LIMITED 0.5, INSUFFICIENT 0), **lowering a stale player's confidence automatically dampens his trend signal — no new machinery.** This prevents a stale player from getting a confident "sell-high / buy-low" trend (Kraft's bogus sell flag, Hunter's bogus buy flag).

### 3b. Staleness → base-level decay (NEW — the actual fix)
Confidence-scaling only touches the *trajectory multiplier*, NOT the base `inseason_level`. Kraft's 70.9 IS the stale base level (prior_w=0 at ≥5 games, so the prior isn't propping him — the stale recency+season blend itself is the inflation). So the new mechanic is a **staleness decay applied to the base level / forward_ppg**, driving it toward the floor as `weeks_stale` grows. This is the genuinely new code; 3a is the safety rail that comes along for free.

---

## 4. Reconciliation with the frozen calibration (#158 / #170 / #172)

Reopening the value engine means stating, explicitly, that this doesn't re-break what's frozen:
- **#158 (season-anchored level, anti-recency-twitchiness):** the bye-skip does NOT increase recency sensitivity to *scoring* — it only removes byes from the window, which is a scheduling correction, not a recency reweighting. A healthy stable player's value is unchanged by either mechanic (staleness=0 → no decay; no byes → no window change). The #158 guard test (stable stud doesn't move) must still hold.
- **#170 (trajectory keyed on usage):** staleness rides the *existing* confidence scale into the trajectory factor (3a). It does not add a new trajectory term — it dampens the existing one for stale players. No double-count.
- **#172 (forward-basis anchors):** the staleness decay applies to the per-player level *before* anchor-scaling, same position in the pipeline as the #170 factors. Anchors are derived from the league's *played* levels and are unaffected by one player's staleness (staleness is per-player, not a pool shift). Confirm the decay doesn't perturb anchor derivation (it shouldn't — anchors should derive from non-stale players' levels; a decayed stale player simply ranks low).

---

## 5. Tunable parameters + conservative defaults

- `_STALENESS_FREE_WEEKS` = 1 (a single non-bye gap → no decay; protects a one-game absence).
- `_STALENESS_DECAY_*` — the decay curve from `weeks_stale` → value multiplier toward floor. Gentle early, steep for prolonged absence. Bounded; conservative default (under-decay before over-decay, per every value-engine change).
- Confidence staleness thresholds — at what `weeks_stale` FULL→LIMITED→INSUFFICIENT.

**Calibration:** re-rate every absent player after the fix; confirm the ~10 inflated injuries crater toward floor, byes-at-edge (CMC, Wan'Dale) are untouched, and healthy players are unchanged. Conservative defaults; tune against the real seed.

---

## 6. Downstream effect (re-rate + a modest unlock)

Like every value-scale change, this propagates to contextual_value / edge band / proposals automatically. Expected effects: (a) phantom Kraft/Hunter trades vanish; (b) ~3 silent teams (The Lord, Break your leg CMC, Fat Bastard) unlock as their inflated lineups deflate to reality and real acquisitions clear cond1; (c) the other ~5 silent teams stay silent (honest gate ceiling — strong/balanced rosters, NOT this bug). Re-eyeball after.

---

## 7. Acceptance tests (the paired safety set)

1. **INJURY CRATERS (headline):** Kraft (out 5 weeks, no byes in span) → forward_value drops from 70.9 toward the floor; the "sell-high" flag is gone (confidence downgraded). He no longer surfaces as a trade target on either side. Same for Tyreek (10w), London (3w).
2. **BYE READS CORRECTLY (Stephen's insight):** a healthy player with a bye in his recent window (e.g. "22, bye, 20") reads his real recent form — bye skipped, window reaches back one played week, value NOT penalized and if anything slightly *higher* than the current bye-diluted handling.
3. **BYE-AT-EDGE UNTOUCHED:** CMC (SF, bye wk14) and Wan'Dale (NYG, bye wk14) — their only post-last-game week is a bye → schedule-aware fix leaves them at full value (proves we don't penalize a bye as if it were an injury).
4. **SINGLE-GAP TOLERATED:** a stud who missed ONE non-bye game (weeks_stale=1) is essentially unchanged (the free-weeks guard).
5. **#158 GUARD (non-negotiable):** a healthy, stable, currently-playing stud (staleness 0, no byes) is unchanged — neither mechanic moves him. Proves we didn't re-break the frozen calibration.
6. **PHANTOM TRADES GONE:** the Big Black Cop "deal Kraft" and Joe Shiesty "acquire Kraft paying Hunter" trades no longer surface.
7. **CONFIDENCE DAMPENS TREND:** a stale player's trajectory factor is dampened (via the downgraded confidence) — no confident sell/buy flag on a month-absent player.

---

## 8. Decisions (for Stephen) + scope

- **8a — bye source: DECIDED** use the existing schedule warehouse (`_get_bye_week` / `player_schedules.bye_week`), not the team-wide-missing inference (noisier — trade + wk13-boundary confounds, 25/32 clean). No new fetch.
- **8b — decay aggressiveness: DECIDED conservative** (gentle curve, only sustained absences crater; still craters true season-enders like Kraft at 5w, protects byes/one-offs; tune up if needed).
- **8c — free-weeks guard: DECIDED `_STALENESS_FREE_WEEKS = 1`** (one non-bye missed game tolerated — a single missed game is common/noisy and shouldn't crater a stud).
- **8d — Demo-fidelity note (separate, not this fix):** even with value fixed, season-enders remain ROSTERED in the demo (a 0-value dead slot) because the demo freezes draft rosters at week 14. Harmless for trades (a floored player won't surface), but it's the demo-churn-fidelity gap — irrelevant once real leagues connect (they pull current rosters). Out of scope; ledgered.

---

## 9. Build sequence

1. **`weeks_stale(player, schedule, current_week)`** — elapsed weeks since last played, minus byes (schedule-driven). Tests against the blast-radius table (Kraft=5, CMC=0-via-bye, etc.).
2. **Bye-aware window** (2a) — recency/usage-trend skip byes and reach back one played week.
3. **Staleness → confidence** (3a) — feed `weeks_stale` into `_assess_confidence`; the trajectory dampening is automatic via existing scaling.
4. **Staleness → base-level decay** (3b) — the new mechanic, applied to inseason_level/forward_ppg before anchor-scaling.
5. **Tests** (§7 paired set) + calibration re-eyeball.

---

## 10. Why this surfaced now

The frozen value engine (#158/#170/#172) was calibrated on *snake-drafted demo data* where rosters were balanced and current — no season-enders sitting on rosters at full value. The real-draft reseed (real week-14 league) is the first data with genuine mid-season injuries, which exposed that the window has no availability/recency notion at all. The fix finishes what the real data revealed: value must know not just *how a player produced* but *whether and when he last played.*
