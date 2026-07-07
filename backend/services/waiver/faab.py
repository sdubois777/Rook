"""
FAAB bid heuristic — transparent + tunable, NOT a Sonnet call.

A suggested Free-Agent-Acquisition-Budget bid is a function of three inputs:
  (a) the add's net lineup improvement in real points/week (the same ppw the
      trade objective computes),
  (b) positional scarcity — how far the add sits above the streamable
      replacement floor at its position (replacement_ppg_by_position),
  (c) faab_remaining — the acting team's remaining budget.

The gain picks a TIER (a % of remaining); scarcity nudges within the tier; the
result is floored at a token $1 for anything worth recommending and capped at
faab_remaining. A fresh news/opportunity signal adds a SEPARATE, transparent bump
(never silently folded into the base bid).

ALL tunable constants live here — one place to retune the curve later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# --- the curve (tune here) ---------------------------------------------------
# Demo league budget. Real leagues will read this from the league settings once
# waiver settings are persisted (explicit follow-up — not built in v1).
FAAB_BUDGET_DEFAULT = 100

# (min net-lineup gain in ppw, % of remaining budget, human tier label).
# Highest tier first; the first tier whose threshold the gain clears wins.
FAAB_TIERS: tuple[tuple[float, float, str], ...] = (
    (5.0, 0.40, "league-winner"),
    (2.5, 0.20, "week-winning starter"),
    (1.0, 0.08, "flex / matchup play"),
    (0.01, 0.02, "speculative stash"),
)

FAAB_MIN_BID = 1            # token floor — anything worth recommending bids >= $1
FAAB_MIN_GAIN = 0.01        # below this ppw gain, don't recommend a bid at all

# Scarcity multiplier: an add well above its position's replacement floor is
# scarcer and worth more of the budget. Bounded so it nudges, never dominates.
SCARCITY_WEIGHT = 0.5       # per 1.0x-of-replacement over the floor
SCARCITY_MULT_MAX = 1.5     # cap the scarcity boost at +50%

# A fresh opportunity/news signal (step 4) adds this % of remaining, shown
# separately from the base bid — never silently folded in.
NEWS_BUMP_PCT = 0.05


@dataclass(frozen=True)
class FaabSuggestion:
    recommended: bool           # False → not worth a bid (gain below floor)
    tier_label: str
    base_pct: float             # % of remaining the tier maps to (after scarcity)
    base_bid: int               # $ from the tiered curve (floored/capped)
    news_bump_bid: int          # additional $ from a fresh news signal (separate)
    total_bid: int              # base + bump, capped at remaining
    pct_of_remaining: float     # total_bid / remaining, for display
    why: str


def suggest_bid(
    *,
    gain_ppw: float,
    faab_remaining: int,
    value_over_replacement: float = 0.0,
    replacement_ppg: float = 0.0,
    has_news_bump: bool = False,
) -> FaabSuggestion:
    """Map (gain, scarcity, remaining) → a suggested FAAB bid. Pure + deterministic.

    ``value_over_replacement`` = add.forward_ppg − replacement_ppg[pos]; scaled by
    ``replacement_ppg`` to a scarcity ratio. ``has_news_bump`` adds the separate
    opportunity bump. The bid never exceeds ``faab_remaining`` and never drops
    below ``FAAB_MIN_BID`` once recommended.
    """
    remaining = max(0, int(faab_remaining))
    if remaining <= 0:
        return FaabSuggestion(
            recommended=False, tier_label="no budget", base_pct=0.0, base_bid=0,
            news_bump_bid=0, total_bid=0, pct_of_remaining=0.0,
            why="no FAAB budget remaining",
        )
    if gain_ppw < FAAB_MIN_GAIN:
        # No immediate lineup gain. A fresh opportunity/breakout signal still makes
        # a token speculative stash worthwhile; otherwise it's not worth a claim.
        if has_news_bump:
            base_bid = min(remaining, FAAB_MIN_BID)
            news_bump_bid = min(remaining - base_bid, round(NEWS_BUMP_PCT * remaining))
            total = base_bid + news_bump_bid
            return FaabSuggestion(
                recommended=True, tier_label="speculative stash",
                base_pct=round(total / remaining, 3), base_bid=base_bid,
                news_bump_bid=news_bump_bid, total_bid=total,
                pct_of_remaining=round(total / remaining, 3),
                why=f"fresh opportunity signal — ${total} speculative stash",
            )
        return FaabSuggestion(
            recommended=False, tier_label="not worth a claim", base_pct=0.0,
            base_bid=0, news_bump_bid=0, total_bid=0, pct_of_remaining=0.0,
            why="does not improve your starting lineup enough to spend on",
        )

    # 1. Tier from the net lineup gain.
    tier_pct, tier_label = FAAB_TIERS[-1][1], FAAB_TIERS[-1][2]
    for threshold, pct, label in FAAB_TIERS:
        if gain_ppw >= threshold:
            tier_pct, tier_label = pct, label
            break

    # 2. Scarcity nudge within the tier (bounded).
    scarcity_ratio = (value_over_replacement / replacement_ppg) if replacement_ppg > 0 else 0.0
    scarcity_mult = min(SCARCITY_MULT_MAX, max(1.0, 1.0 + SCARCITY_WEIGHT * scarcity_ratio))
    eff_pct = tier_pct * scarcity_mult

    # 3. Base bid: floored at the token bid, capped at remaining.
    base_bid = max(FAAB_MIN_BID, round(eff_pct * remaining))
    base_bid = min(base_bid, remaining)

    # 4. News bump (separate + transparent), total capped at remaining.
    news_bump_bid = round(NEWS_BUMP_PCT * remaining) if has_news_bump else 0
    total_bid = min(remaining, base_bid + news_bump_bid)
    # If the bump pushed against the cap, the shown bump is what actually fit.
    news_bump_bid = total_bid - base_bid if total_bid > base_bid else 0

    why = f"{tier_label}: ~{round(eff_pct * 100)}% of your ${remaining} remaining"
    if scarcity_mult > 1.0:
        why += f" (scarce at {int(round(value_over_replacement))}+ ppw over replacement)"
    if news_bump_bid:
        why += f"; +${news_bump_bid} for the fresh opportunity signal"

    return FaabSuggestion(
        recommended=True, tier_label=tier_label, base_pct=round(eff_pct, 3),
        base_bid=base_bid, news_bump_bid=news_bump_bid, total_bid=total_bid,
        pct_of_remaining=round(total_bid / remaining, 3) if remaining else 0.0,
        why=why,
    )
