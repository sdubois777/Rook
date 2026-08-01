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

Leagues that claim by waiver PRIORITY rather than bidding pass
``faab_remaining=None``. Everything except the money still applies to them — the
ranking is unchanged and the tier label still says how much the add is worth — so
those leagues get the tier with every dollar field zeroed and ``bid_applicable``
False. Inventing a dollar figure for a league that never bids is the exact defect
this module was rewritten to stop.

ALL tunable constants live here — one place to retune the curve later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# --- the curve (tune here) ---------------------------------------------------
# The STATED ASSUMPTION used only when a league bids but its budget could not be
# read, and the demo league's budget. It is no longer a stand-in for real league
# settings: those are read from the platform during sync and stored on
# user_leagues.waiver_budget. Anywhere this constant reaches a customer, the
# response also carries budget_is_assumed=True and the page must say so.
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
    # False when the league claims by waiver priority instead of bidding. Every
    # dollar field is then 0 and means "not applicable", NOT "$0" — a caller that
    # renders the number anyway prints a figure the league does not have.
    bid_applicable: bool = True


def _tier_for(gain_ppw: float) -> tuple[float, str]:
    """(% of remaining, human label) for a net lineup gain. Highest tier first;
    the first tier whose threshold the gain clears wins."""
    for threshold, pct, label in FAAB_TIERS:
        if gain_ppw >= threshold:
            return pct, label
    return FAAB_TIERS[-1][1], FAAB_TIERS[-1][2]


def _priority_league_suggestion(gain_ppw: float, has_news_bump: bool) -> FaabSuggestion:
    """The suggestion for a league that does NOT bid (waiver priority / reverse
    standings). Same tier, same worth-claiming decision, no money."""
    if gain_ppw < FAAB_MIN_GAIN and not has_news_bump:
        return FaabSuggestion(
            recommended=False, tier_label="not worth a claim", base_pct=0.0,
            base_bid=0, news_bump_bid=0, total_bid=0, pct_of_remaining=0.0,
            why="does not improve your starting lineup enough to claim",
            bid_applicable=False,
        )
    if gain_ppw < FAAB_MIN_GAIN:
        label = "speculative stash"
        why = "fresh opportunity signal — worth a speculative claim"
    else:
        label = _tier_for(gain_ppw)[1]
        why = f"{label} — worth spending waiver priority on"
        if has_news_bump:
            why += "; fresh opportunity signal"
    return FaabSuggestion(
        recommended=True, tier_label=label, base_pct=0.0, base_bid=0,
        news_bump_bid=0, total_bid=0, pct_of_remaining=0.0, why=why,
        bid_applicable=False,
    )


def suggest_bid(
    *,
    gain_ppw: float,
    faab_remaining: Optional[int],
    value_over_replacement: float = 0.0,
    replacement_ppg: float = 0.0,
    has_news_bump: bool = False,
) -> FaabSuggestion:
    """Map (gain, scarcity, remaining) → a suggested FAAB bid. Pure + deterministic.

    ``value_over_replacement`` = add.forward_ppg − replacement_ppg[pos]; scaled by
    ``replacement_ppg`` to a scarcity ratio. ``has_news_bump`` adds the separate
    opportunity bump. The bid never exceeds ``faab_remaining`` and never drops
    below ``FAAB_MIN_BID`` once recommended.

    ``faab_remaining=None`` means the league does not bid at all. It returns the
    tier with no dollar figure rather than raising — which is what it used to do,
    AFTER the caller had already charged the customer 2 credits with no refund path.
    """
    if faab_remaining is None:
        return _priority_league_suggestion(gain_ppw, has_news_bump)

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
    tier_pct, tier_label = _tier_for(gain_ppw)

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
