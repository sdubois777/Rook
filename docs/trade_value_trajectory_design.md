# Trade Value — Usage-Trajectory & Opportunity-Gap Wiring

**Status:** IMPLEMENTED (this doc is the build spec). Wires the founding
differentiator — usage TRAJECTORY and efficiency mean-reversion — into
`forward_value` itself, where it was previously computed, displayed as a
buy/sell flag, and then **ignored by the value math**.

---

## 1. The bug this fixes

`docs/trade_agent_design.md §3` promised value driven by **usage trajectory**, not
preseason reputation or raw point level. The value engine *computed* the trend
(`value_trend`, `buy_low`, `sell_high`) and *displayed* it — but `forward_value`
was a pure **level** blend (recency-weighted recent PPR + season PPR, scaled by
positional anchors). The trajectory touched value only through two narrow
FALLING-only valves (`unsustainable_hot` regression; the name-bias prior
discount). Buy-low **never raised** value; a falling role was **not discounted**
unless it was also scoring above volume.

Consequence (the report that triggered this): acting as Blitz Brigade, the engine
rated *give rising buy-low Burrow (33) / get falling sell-high Henderson (47)* as
**"+13.2 you win"** — buy-high / sell-low scored as a win — because it compared
*levels* and the trajectory was decorative. The analyzer prose then read backwards
because it was handed a verdict that contradicted the flags.

## 2. Goal & principle

Make the trajectory and the opportunity-vs-production gap **actually move
`forward_value`**, symmetrically and conservatively, so a rising/under-producing
player reads higher (acquire target) and a falling/over-producing player reads
lower (trade away) — while a stable-usage player is **untouched** (#158 safety).

Principle (§7e target, linearized for v1): *forward production should track usage
and volume.* Two multiplicative factors on `forward_ppg` are a clean v1
linearization of that target; the fully-principled "project usage forward, compute
expected production at projected usage" is deferred (§7e).

---

## 3. The two factors

### 3.1 Usage-trajectory factor (role change)
Driven by the composite USAGE trend (last-2-weeks vs prior-3, target share + snap %) — the same signal that sets buy/sell flags. NOT by recent scoring.
- Centered at 1.0 (no trend → factor 1.0 → **untouched**, the #158 safety).
- **Symmetric** — rising usage lifts (>1), falling discounts (<1). Buy-low currently does NOTHING; this is half the differentiator, dead.
- **Bounded** by a hard cap (the #158 volatility guardrail).
- **Confidence-scaled** — full → full adjustment, limited → dampened, insufficient → ~none (prevents a thin/returning-player trend from whipsawing value; the Lamar safety).

### 3.2 Opportunity-vs-production-gap factor (efficiency mean-reversion)
Forward production should regress toward **volume-implied** production: high-volume/low-output → lift toward implied (buy-low); low-volume/high-output (esp. TD-driven) → discount toward implied (sell-high). Symmetric, bounded, confidence-scaled.
- This is the GENERAL form of the existing `unsustainable_hot` valve, which is only the falling-and-over-producing corner. §4 reconciles.
- Only applies where volume is MEASURED (targets + carries). QBs (passing volume not captured) and players with no volume data get NO opp-gap adjustment — otherwise expected≈0 makes every QB a phantom over-producer and craters them.

### 3.3 Shape / composition
- Multiplicative on `forward_ppg`, before anchor-scaling. Right shape (role change matters more in absolute terms for high-value players); a clean linearization of the principled "production tracks usage/volume" target (§7e).
- The two factors are **independently tunable** (separate constants, each settable to 0). This preserves clean attribution during calibration — isolate one signal at a time even though both ship.

---

## 4. Composition with existing mechanisms (reconciliation — done)

- **`unsustainable_hot`** (falling + scoring ≥4 PPR above volume) is the
  falling/over-producing CORNER of the §3.2 opp-gap signal. The symmetric opp-gap
  factor **SUBSUMES** it: the old direct `in_season_ppg` regression-toward-volume
  is **removed** (it would double-discount on top of the opp-gap factor). The
  `unsustainable_hot` / `sustainable` booleans are still computed for the
  **sell-high flag + transparency**; a falling over-producer is now discounted via
  the general opp-gap factor (still discounts → preserved as a special case, §8.6).
- **Name-bias prior discount** (falling + prior propping value) is **KEPT**, not
  folded. It is a distinct mechanism: it down-weights the PRESEASON PRIOR
  (reputation) inside the level blend, whereas the trajectory factor nudges the
  post-blend `forward_ppg` by usage direction. They overlap only for a
  falling-player-with-a-propping-prior; conservative defaults + the combined cap
  bound the total. Fold-in is a calibration follow-up if over-discounting shows up.
- **Combined cap:** the trajectory × opp-gap PRODUCT is clamped (not just each
  factor), so two same-direction signals can't over-crater a value.

---

## 5. Tunable parameters + calibration plan

- `_TRAJECTORY_COEFFICIENT = 0.5`, `_TRAJECTORY_CAP = 0.12` — usage-trajectory strength + bound.
- `_OPP_GAP_WEIGHT = 0.012`, `_OPP_GAP_CAP = 0.10` — opp-gap regression strength + bound.
- `_OPP_GAP_MIN_EXPECTED = 1.0` — volume floor below which opp-gap is skipped (QB guard).
- `_LIMITED_TREND_SCALE = 0.5`; INSUFFICIENT / team-change → 0.0 (confidence scaling).
- `_COMBINED_FACTOR_BOUNDS = (0.80, 1.20)` — clamp on the product.
- **Independently tunable:** setting either coefficient to 0 disables that signal cleanly, for isolated calibration.

**Defaults ship CONSERVATIVE (decision 7a)** — trajectory/opp-gap NUDGE; the level still dominates. Better to under-move than recreate #158 twitchiness; tune UP against real output.

**Calibration plan:** after wiring, restart and eyeball the real league — (a) rising/under-producing players read higher, surface as acquire targets; (b) falling/over-producing read lower, surface as trade-away; (c) **elite stable-usage players must not move** (#158 check); (d) Burrow/Henderson flips. Tune against (a)-(d), one signal at a time if needed (§3.3 independence).

---

## 6. Downstream propagation (calibration resets)

`forward_value` is the primitive everything is built on (`contextual_value`, edge band, analyzer, proposals) — wiring propagates correctly to all with NO per-consumer change. But every value shifts: buy-lows up, sell-highs down, so **which trades surface changes** (the differentiator working, not a regression). **Re-judge `_COMFORT_THRESHOLD` (currently 2.0) after this lands** — every prior calibration look was on level-only values.

---

## 7. Decisions (made) + deferred

- **7a — Aggressiveness: DECIDED conservative** (small coefficients, level dominates, tune up).
- **7b — Wire both signals: DECIDED yes**, usage-trajectory AND opp-gap, independently tunable for clean attribution.
- **7c — Symmetry: DECIDED yes** (rising lifts, not just falling-discount).
- **7d — Multiplicative-on-forward_ppg: DECIDED yes.**
- **7e — Deferred v2:** the fully-principled "project forward usage, compute expected production at projected usage." The multiplicative factors are a v1 linearization. Ledgered.

---

## 8. Acceptance tests (the paired safety property — all must hold together)

1. **BURROW/HENDERSON FLIPS (headline):** give rising buy-low, get falling sell-high → verdict no longer "+13.2 you win." Burrow lifts, Henderson discounts, delta shrinks toward zero or reverses. Assert direction changed.
2. **#158 GUARD — stable-usage stud in a scoring dip STAYS PUT (non-negotiable):** high-value player, stable usage, a couple low scoring weeks → factors ≈ 1.0, forward_value essentially unchanged. Proves keyed-on-usage, not scoring; #158 intact.
3. **CONFIDENCE GUARD — thin-sample trend does NOT whipsaw (Lamar safety):** returning/low-games player with a noisy usage trend → confidence-scaling dampens to ~none.
4. **OPP-GAP under-producer lifts:** high volume, low output → forward_value lifts toward volume-implied (buy-low).
5. **OPP-GAP over-producer discounts:** low volume, high TD output → discounts toward volume-implied (sell-high).
6. **`unsustainable_hot` PRESERVED as a special case:** the old falling/over-producing case still discounts (now via the general opp-gap), not lost in the subsumption.
7. Symmetric lift/discount tests; cap tests (extreme swing clamped); combined-downward cap (§4) doesn't over-crater.

---

## 9. Why this is late, stated plainly

The signals were supposed to be wired from the original trade-agent design (§3 — the differentiator) but weren't: flags built and displayed, never fed into value. Found because a human read the rationale against the flags and saw it was backwards. The level work (#158, #160) was real and correct — it only ever calibrated the *level*, never the *trajectory* or *efficiency*. This finishes the differentiator the original design promised.
