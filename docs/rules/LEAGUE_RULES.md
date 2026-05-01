# League Rules and Draft Optimization Strategy

This document defines the hard constraints of the user's fantasy league and
the strategic framework the live draft agent uses to build an optimal roster.

All code that references league settings must import from the `league_settings`
table in the database — never hardcode these values.

---

## League Settings

```python
LEAGUE_SETTINGS = {
    "platform": "Yahoo",
    "format": "PPR",                    # Points Per Reception
    "team_count": 12,
    "auction_budget": 200,              # Total budget per team
    "min_bid": 1,                       # Minimum bid on any player
}
```

---

## Roster Construction

```python
ROSTER_SLOTS = {
    "QB":    1,
    "RB":    2,
    "WR":    2,
    "FLEX":  1,    # RB / WR / TE eligible
    "TE":    1,
    "K":     1,
    "DEF":   1,
    "BENCH": 7,
}

TOTAL_ROSTER_SIZE = 16   # Sum of all slots above
STARTING_LINEUP_SIZE = 9 # QB + RB + RB + WR + WR + TE + FLEX + K + DEF
```

---

## Budget Framework

**Everyone has $200. The strategy is how you allocate it.**

```python
AUCTION_BUDGET = 200       # Every team's total budget — no exceptions
MIN_BID       = 1          # Minimum bid on any player
TOTAL_ROSTER  = 16         # Total players drafted
```

### The $185 / $15 split

This is a **spending strategy**, not an accounting trick.

```python
# Target: spend this much on your 7 starting SKILL POSITION players
SKILL_STARTER_BUDGET = 185   # QB + RB1 + RB2 + WR1 + WR2 + TE + FLEX

# Reserve this much for everything else
BENCH_AND_SPECIALIST_BUDGET = 15   # K + DEF + 7 bench spots (9 players)
```

The 9 non-skill-starter spots (K, DEF, 7 bench) are almost always
$1-2 each. Spending $15 on them is generous — you'll likely spend $10-12
and have a few dollars left over. The point is: **do not spend real money
on K, DEF, or bench players**. That budget belongs to your starters.

```
Starting skill lineup (7 spots) — target $185 total:
  QB:      $15-35
  RB1:     $50-75   ← most expensive position in PPR
  RB2:     $20-40
  WR1:     $40-60
  WR2:     $15-30
  TE:      $15-30
  FLEX:    $10-25   ← whoever's best available after the above

Non-skill starters + bench (9 spots) — stay under $15 total:
  K:       $1-2
  DEF:     $1-2
  Bench ×7: $1-3 each
```

### Why this framing matters for the code

The valuation engine must calibrate player dollar values against
what teams actually spend on starting skill positions — not total budget.

```python
# CORRECT calibration pool:
# 12 teams × $185 skill starter budget = total league dollars for skill starters
LEAGUE_SKILL_DOLLAR_POOL = SKILL_STARTER_BUDGET * LEAGUE_TEAM_COUNT
# = 185 × 12 = $2,220

# WRONG (do not use):
# LEAGUE_SKILL_DOLLAR_POOL = AUCTION_BUDGET * LEAGUE_TEAM_COUNT = 200 × 12 = $2,400
# This inflates every player's dollar value by ~8%
```

Player dollar value is then:
```python
# player_value = (player_surplus_points / total_surplus_points) × $2,220
#
# surplus = projected_ppr_points_per_game - replacement_level
REPLACEMENT_LEVEL_PPR = {
    "QB":  18.0,   # Streamable QB off waivers
    "RB":   8.0,   # Waiver wire RB
    "WR":   7.0,   # Waiver wire WR
    "TE":   5.0,   # Streamable TE
}
```

### Individual bid ceiling sanity checks

These are the MAXIMUM realistic prices in a $200 12-team PPR auction.
**Any bid ceiling above $80 is a calculation error — stop and debug.**

```python
MAX_REALISTIC_BID = {
    "RB":  80,    # Only the most elite RB in a perfect situation
    "WR":  70,    # Only the most elite WR
    "QB":  50,    # Only if going QB-first strategy
    "TE":  45,    # Only Bowers/Andrews tier
    "K":    2,    # Never more than $2
    "DEF":  2,    # Never more than $2
}

# Typical ranges for competitive players:
TYPICAL_BID_RANGES = {
    "RB1":  (50, 75),
    "RB2":  (20, 40),
    "WR1":  (40, 60),
    "WR2":  (15, 30),
    "QB1":  (20, 40),
    "TE1":  (20, 35),
    "FLEX": (10, 25),
}
```

---

## Positional Budget Allocation

Historical PPR auction data shows roughly how budgets split across positions.
These are **targets and soft constraints** — not hard limits.
The live draft agent uses them as a guide, adjusting based on actual auction flow.

```python
POSITIONAL_BUDGET_TARGETS = {
    # As percentage of SKILL_POSITION_BUDGET ($183)
    "RB":  0.38,    # ~$70 — most scarce position, highest value at top
    "WR":  0.32,    # ~$59 — deep position, value extends further down
    "QB":  0.10,    # ~$18 — one starter, often efficient to wait
    "TE":  0.10,    # ~$18 — elite TE is valuable, rest are streamers
    "K":   0.01,    # ~$2  — always $1, sometimes $2 for elite K
    "DEF": 0.01,    # ~$2  — always $1, sometimes $2 for elite DEF
    # Remaining ~8% = bench flexibility
}
```

**Important:** These percentages reflect market averages. The system should
track actual auction spend vs targets in real time and alert when a position
is running significantly over budget — e.g. "You've spent $85 on RB already,
$15 over target. Adjust WR budget accordingly."

---

## Lineup Optimization Goal

The objective is not to draft the highest-value players available.
The objective is to draft the **best possible starting lineup**.

This distinction matters because:

- A $60 RB1 with a $55 WR1 leaves $70 for 13 more players — very hard to
  field a competitive starting lineup
- A $45 RB1 with a $40 WR1 leaves $100 for 13 more players — much more
  flexibility to build depth and a strong flex

The optimizer should always evaluate a proposed bid in the context of
"what does my starting lineup look like when this draft ends?" not
"is this player worth this price in a vacuum?"

---

## Starting Lineup Value Targets

These are the minimum projected PPR scores the system should target for a
competitive starting lineup. If a lineup projection falls below these, the
draft strategy needs adjustment.

```python
COMPETITIVE_LINEUP_TARGETS = {
    # Minimum projected PPR points per week for a competitive team
    "QB":   18.0,   # Game manager floor — anything above is upside
    "RB1":  14.0,   # True starter
    "RB2":  10.0,   # Solid second option
    "WR1":  13.0,   # True starter
    "WR2":   9.0,   # Solid second option
    "FLEX":  9.0,   # Interchangeable with WR2/RB2 tier
    "TE":    8.0,   # Tight end is notoriously volatile
    "K":     7.0,   # Kicker baseline
    "DEF":   7.0,   # Defense baseline
    
    # Total floor for competitive weekly score
    "TOTAL_FLOOR": 95.0,
}
```

---

## Roster Construction Strategies

The live draft agent should recognize and adapt to these common auction
strategies. It should also detect which strategy opponents are pursuing
and factor that into nomination and blocking decisions.

### Hero RB (Recommended for this league)

**Philosophy:** Spend heavily on one elite RB, build the rest efficiently.

```
RB1: $50-70  (tier 1)
RB2: $15-25  (tier 2-3)
WR1: $35-50  (tier 2)
WR2: $15-25  (tier 3)
QB:  $10-20  (tier 2-3, or wait)
TE:  $15-25  (tier 1-2, or $1 streamer)
```

**Why it works in PPR:** Elite RBs with pass-catching roles get massive PPR
boosts. A true three-down back with target share is the scarcest commodity
in PPR formats. The position drops off sharply after tier 1.

**Risk:** If your RB1 gets injured early, the season is likely over. Injury
risk modifier is especially critical for hero RB targets.

### Zero RB

**Philosophy:** Skip RBs in early rounds, load up on WRs, pick up RBs on
the waiver wire.

```
WR1: $45-60  (tier 1)
WR2: $35-45  (tier 1-2)
WR3: $20-30  (tier 2)
QB:  $20-35  (tier 1-2)
TE:  $20-30  (tier 1)
RBs: $5-15 each (committee backs, handcuffs)
```

**When to pursue:** When top RBs are clearly overpriced (market_value >>
system_value), or when RB injury risk is high across the board.

**Risk:** Waiver wire RBs are unreliable. This strategy requires active
in-season management to work.

### Balanced / Stars and Scrubs

**Philosophy:** Spend on 2-3 elite players at premium prices, fill the
rest with $1-5 players.

```
Star 1: $60-80
Star 2: $40-55
Star 3: $25-35
Everyone else: $1-8
```

**When to pursue:** When specific value targets are identified — e.g. a
clear case where market undervalues a player by $15+.

---

## Budget Tracking During Live Draft

The live draft agent must track these metrics after EVERY pick:

```python
class LiveBudgetState:
    auction_budget: int           # Always 200
    spent: int                    # Total spent so far
    remaining: int                # 200 - spent
    
    roster_slots_filled: int      # How many players drafted
    roster_slots_remaining: int   # 15 - slots_filled
    
    minimum_completion_budget: int  # slots_remaining × $1
    
    spendable: int               # remaining - minimum_completion_budget
    
    # Positional spend tracking
    rb_spent: int
    wr_spent: int
    qb_spent: int
    te_spent: int
    
    # Position slots still needed
    rb_slots_needed: int
    wr_slots_needed: int
    qb_slots_needed: int
    te_slots_needed: int
    
    # Projected lineup quality
    projected_rb1_score: float
    projected_rb2_score: float
    projected_wr1_score: float
    projected_wr2_score: float
```

### Budget alerts to surface in UI

The live draft agent should emit a budget alert to the UI when:

```python
BUDGET_ALERTS = [
    {
        "condition": "spendable < 20 and slots_remaining > 6",
        "severity": "critical",
        "message": "Budget critically low — only ${spendable} for {slots} spots. Pass on everything above $3."
    },
    {
        "condition": "rb_spent > 90",
        "severity": "warning",
        "message": "RB spend ${rb_spent} is significantly over target (${target}). Redirect budget to WR."
    },
    {
        "condition": "roster_slots_remaining <= 3 and spendable > 30",
        "severity": "info",
        "message": "${spendable} remaining for {slots} spots — opportunity to spend up on remaining targets."
    },
    {
        "condition": "qb_slots_needed > 0 and roster_slots_remaining <= 4",
        "severity": "warning",
        "message": "No QB drafted yet with {slots} spots left — prioritize QB soon."
    }
]
```

---

## Bid Ceiling Adjustment for Budget State

The bid ceiling formula in `docs/ARCHITECTURE.md` produces a price based on
player value. This must be further constrained by current budget state:

```python
def apply_budget_constraint(
    raw_bid_ceiling: float,
    budget_state: LiveBudgetState,
    player_tier: int,
    player_position: str
) -> float:
    """
    Never recommend a bid that would prevent roster completion.
    Adjust ceiling based on remaining budget and positional needs.
    """
    
    # Hard ceiling: never exceed spendable budget
    ceiling = min(raw_bid_ceiling, budget_state.spendable)
    
    # If this is a non-essential position with roster gaps elsewhere, limit spend
    # e.g. don't spend $30 on a WR3 if you still need a QB and TE
    essential_gaps = (
        budget_state.qb_slots_needed +
        budget_state.te_slots_needed
    )
    if essential_gaps > 0 and player_tier >= 3:
        # Reserve $15 per essential gap for those positions
        ceiling = min(ceiling, budget_state.spendable - (essential_gaps * 15))
    
    # Minimum $1 always
    return max(1, ceiling)
```

---

## Optimal Team Construction — Target Ranges

At the end of the draft, a competitive team should look like this.
The live draft agent uses this as a reference when evaluating whether
the current roster build is on track.

```
Position | Target spend | Min acceptable score
---------|-------------|---------------------
QB       | $10-25      | 18 pts/week
RB1      | $35-65      | 14 pts/week
RB2      | $12-25      | 10 pts/week
WR1      | $30-55      | 13 pts/week
WR2      | $12-25      |  9 pts/week
FLEX     |  $8-20      |  9 pts/week
TE       |  $8-30      |  8 pts/week
K        |   $1-2      |  7 pts/week
DEF      |   $1-2      |  7 pts/week
Bench ×7 |  $1-5 each  |  depth/handcuffs

Total starting spend target: ~$120-160
Bench spend: ~$15-30 (mostly $1-3 fliers)
K + DEF: $2-4
```

The system should flag if the user's current draft trajectory is headed
toward missing any "min acceptable score" threshold for a starting position.

---

## Bench Strategy

In a 12-team PPR league with 7 bench spots, bench construction follows
this priority order:

1. **Handcuffs** — backup RBs for your starting RBs. If your RB1 goes down,
   their handcuff is an immediate starter. Target price: $1-3.

2. **High-upside fliers** — players with clear path to expanded role if
   something changes (backup QB on passing team, WR2 behind aging WR1).
   Target price: $1-5.

3. **Bye week coverage** — at least one player per starting position
   that covers your starters' bye weeks.

4. **Injury insurance** — if you spent heavily on injury-risk players
   (post-ACL, chronic soft tissue), prioritize their replacements.

The live draft agent should track bench construction and flag gaps:
- "You have no handcuff for [RB1]"
- "You have no QB backup — consider grabbing one in the last few rounds"

---

## Flex Slot Optimization

The FLEX slot (RB/WR/TE eligible) should be filled with whoever projects
highest among non-starting eligible players. However:

- In PPR, WR tends to win the flex spot more often than RB due to
  higher target volume
- A pass-catching RB on a high-implied team can be flex-worthy
- Elite TEs (Bowers tier) can flex over WR2s in favorable matchups

The lineup optimizer should evaluate FLEX weekly based on that week's
specific matchup and Vegas context, not just season averages.

---

## Rules for Claude Code

1. **`SPENDABLE_BUDGET = 185` is the TOTAL draft budget, not a per-player limit.**
   A bid ceiling above $80 is almost certainly a calculation error. If any
   player's bid ceiling exceeds $80, stop and verify the calibration math.

2. **All league settings come from the `league_settings` table** — never
   hardcode budget, roster slots, or team count in application code.

3. **Dollar values are calibrated against the LEAGUE skill starter dollar pool.**
   Use `SKILL_STARTER_BUDGET × LEAGUE_TEAM_COUNT = 185 × 12 = $2,220`
   as the total pool for surplus value mapping.
   - WRONG: `200 × 12 = $2,400` (total budget, not skill starter budget)
   - WRONG: `183 × 12 = $2,196` (previous incorrect value)
   - CORRECT: `185 × 12 = $2,220`

4. **Positional budget allocation targets from LEAGUE_RULES.md:**
   RB=38%, WR=32%, QB=10%, TE=10% of SKILL_POSITION_BUDGET.
   Do NOT invert WR and QB. QB is 10%, not 38%.

5. **`apply_budget_constraint()` runs last** before emitting any bid
   recommendation. It verifies the roster can be completed competitively
   after spending the proposed amount.

6. **Baseline stats must be per-season averages, not cumulative sums.**
   If a player shows 2,800+ rushing yards, that is a multi-season sum.
   Average across clean seasons — never sum them.

7. **PPR points formula:** `receptions × 1 + yards × 0.1 + TDs × 6`
   If stored ppr_points diverge from this formula by more than 5 points,
   there is a double-counting bug. Fix the Player Profiles agent.

8. **Track positional budget vs target in real time** — alert when any
   position is significantly over target, not just when total budget is low.

9. **K and DEF are always last** — never spend more than $2 on either.
   Never recommend meaningful budget for K or DEF under any circumstances.

10. **The goal is the best starting lineup, not the highest-value roster.**
    Always evaluate bids in the context of "what does my full starting lineup
    look like when this draft ends?"
