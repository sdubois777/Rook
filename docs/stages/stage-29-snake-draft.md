# Stage 29: Snake Draft Support

## Before starting, read:
- `docs/stages/stage-25-saas-foundation.md` (must be complete)
- `docs/LIVE_DRAFT.md`
- `docs/LEAGUE_RULES.md`
- `docs/stages/stage-12-live-draft.md` (auction draft agent for reference)

---

## Goal
Full snake draft support: a pre-draft ranking board optimized for
snake format, and a live draft agent that gives real-time pick
recommendations during the draft. Snake users see ADP-adjusted
rankings and "value over ADP" signals instead of bid ceilings.

---

## How snake draft value works

In auction: system outputs bid ceilings (max you should pay)
In snake: system outputs pick recommendations (best available at this pick)

The underlying player analysis is identical — profiles, flags,
projections, dependency flags all carry over. Only the output
layer changes.

```
Auction output:
  "Chase: bid up to $70, let go at $78"

Snake output:
  "Chase: ADP 1.3 — take at 1.04, don't reach past 1.01"
```

**Value metric for snake: Value Over Expected (VOE)**
```
VOE = player_rank - adp_rank
Positive VOE = player available later than expected (value)
Negative VOE = reaching earlier than expected
```

---

## Part 1 — ADP data source

FantasyPros already integrated. Add ADP endpoint:

```python
# backend/integrations/fantasypros.py

async def get_snake_adp(
    season: int,
    scoring: str = "ppr",
    platform: str = "yahoo",
    team_count: int = 12,
) -> pd.DataFrame:
    """
    Get snake draft ADP from FantasyPros.
    Returns player rankings with ADP and position rank.
    
    platform affects ADP: Yahoo and ESPN have different
    player pools and draft tendencies.
    """
    platform_map = {
        "yahoo": "yahoo",
        "espn": "espn",
        "sleeper": "sleeper",
    }
    
    url = (
        f"https://api.fantasypros.com/v2/json/nfl/"
        f"{season}/consensus-rankings"
        f"?type=overall"
        f"&scoring={scoring.upper()}"
        f"&platform={platform_map.get(platform, 'yahoo')}"
    )
    
    response = await fetch_with_retry(url, headers=FP_HEADERS)
    players = response.get("players", [])
    
    return pd.DataFrame([
        {
            "player_name": p["player_name"],
            "position": p["player_position_id"],
            "team": p["player_team_id"],
            "adp": float(p.get("avg_pick", 999)),
            "adp_std_dev": float(p.get("std_dev", 0)),
            "position_rank": p.get("pos_rank"),
            "overall_rank": p.get("rank_ecr"),
        }
        for p in players
        if p.get("player_position_id") in 
           ("QB", "RB", "WR", "TE", "K", "DST")
    ])
```

Store ADP in players table:
```python
# Add columns to players table
adp_overall     NUMERIC  -- overall ADP rank
adp_positional  NUMERIC  -- positional ADP rank
adp_std_dev     NUMERIC  -- ADP variance (higher = more variable)
adp_platform    VARCHAR  -- yahoo | espn | sleeper
adp_scoring     VARCHAR  -- ppr | half_ppr
adp_updated_at  TIMESTAMP
```

---

## Part 2 — Snake valuation engine

```python
# backend/engines/snake_valuation.py

class SnakeValuationEngine:
    """
    Converts PAR surplus values and AI projections
    to snake draft pick recommendations.
    """
    
    def compute_snake_rankings(
        self,
        players: list[Player],
        profiles: dict[str, PlayerProfile],
        config: LeagueConfig,
    ) -> list[SnakeRanking]:
        """
        For each player, compute:
        - system_rank: our projected PPR rank
        - adp_rank: consensus pick position
        - voe: value over expected (system_rank - adp_rank)
          positive = available later than expected
        - positional_scarcity: how many starters remain
          at this position after this pick window
        - tier_value: is this a tier-defining pick?
        """
        rankings = []
        
        for player in players:
            profile = profiles.get(str(player.id))
            proj_ppr = self._get_projected_ppr(player, profile)
            
            if not proj_ppr:
                continue
            
            system_rank = self._get_system_rank(
                player, players, config
            )
            adp_rank = float(player.adp_overall or 999)
            voe = adp_rank - system_rank
            # Positive = system ranks higher than consensus
            
            # Tier classification for snake
            snake_tier = self._compute_snake_tier(
                player, system_rank, adp_rank, config
            )
            
            rankings.append(SnakeRanking(
                player=player,
                proj_ppr=proj_ppr,
                system_rank=system_rank,
                adp_rank=adp_rank,
                voe=round(voe, 1),
                adp_std_dev=float(
                    player.adp_std_dev or 0
                ),
                snake_tier=snake_tier,
                positional_scarcity=self._compute_scarcity(
                    player.position, system_rank, config
                ),
            ))
        
        return sorted(rankings, key=lambda r: r.system_rank)
    
    def _compute_snake_tier(
        self,
        player: Player,
        system_rank: int,
        adp_rank: float,
        config: LeagueConfig,
    ) -> str:
        """
        Snake tiers are pick-window based, not dollar-based.
        
        elite:    system rank 1-6 at position — take immediately
        strong:   system rank 7-18 — target in mid rounds
        value:    voe > 15 — significant ADP discount
        reach:    voe < -10 — going earlier than expected
        depth:    system rank 25+ — late round flier
        """
        if system_rank <= 6 * config.team_count / 12:
            return "elite"
        if system_rank <= 18 * config.team_count / 12:
            return "strong"
        if adp_rank - system_rank > 15:
            return "value"
        if system_rank - adp_rank > 10:
            return "reach"
        return "depth"
    
    def _compute_scarcity(
        self,
        position: str,
        system_rank: int,
        config: LeagueConfig,
    ) -> str:
        """
        How many startable players remain at this position
        after this pick window closes?
        
        scarce:   < 25% of startable players remain
        limited:  25-50% remain  
        available: > 50% remain
        """
        starter_count = {
            "WR": config.wr_replacement_rank,
            "RB": config.rb_replacement_rank,
            "QB": config.qb_replacement_rank,
            "TE": config.te_replacement_rank,
        }.get(position, 20)
        
        remaining_pct = max(
            0, (starter_count - system_rank) / starter_count
        )
        
        if remaining_pct < 0.25:
            return "scarce"
        if remaining_pct < 0.50:
            return "limited"
        return "available"
```

---

## Part 3 — Snake draft board UI

`frontend/src/pages/SnakeDraftBoard.jsx`

Different from auction draft board — shows pick recommendations
instead of bid ceilings.

```
SNAKE DRAFT BOARD
Your pick position: 4 of 12

Strategy: [Balanced ▼]    Position: [All ▼]    [🔍 Search]

TIER 1 — ELITE (Take immediately if available)
┌────┬──────────────────┬─────┬──────┬──────┬────────┬──────────────┐
│Rank│ Player           │ Pos │ ADP  │ VOE  │ Proj   │ Status       │
├────┼──────────────────┼─────┼──────┼──────┼────────┼──────────────┤
│  1 │ Ja'Marr Chase    │ WR  │ 1.2  │ 0    │ 342 PPR│ ─            │
│  2 │ CMC              │ RB  │ 1.4  │ 0    │ 414 PPR│ ─            │
│  3 │ Bijan Robinson   │ RB  │ 2.1  │ +0.9 │ 368 PPR│ ↑ VALUE      │
│  4 │ Puka Nacua       │ WR  │ 3.4  │ +0.4 │ 377 PPR│ ─            │
│  5 │ Jahmyr Gibbs     │ RB  │ 2.8  │ -1.2 │ 366 PPR│ ─            │
│  6 │ Josh Allen       │ QB  │ 4.1  │ +0.1 │ 362 PPR│ ─            │
└────┴──────────────────┴─────┴──────┴──────┴────────┴──────────────┘

TIER 2 — STRONG (Good value in rounds 2-5)
...

At pick 1.04 — System recommends: CeeDee Lamb
"Chase and CMC likely gone at 1-2. Bijan at 1.03 is possible.
 If Bijan gone, Lamb at 1.04 is fair ADP. Best WR available.
 Avoid reaching for Allen — plenty of QB value in round 8+."
```

**VOE display:**
- Green `↑ VALUE +N` when player available significantly later than ADP
- Red `↓ REACH -N` when player typically goes earlier
- Gray `─` when within 2 picks of ADP

---

## Part 4 — Live snake draft agent

The live draft agent for snake is simpler than auction:
- No budget tracking
- No nomination strategy
- No bid timing
- Just: given available players and your roster, who do you take?

```python
# backend/agents/snake_draft_agent.py

class SnakeDraftAgent:
    """
    Real-time snake draft pick recommendations.
    Runs on each pick via WebSocket.
    """
    
    async def get_pick_recommendation(
        self,
        available_players: list[Player],
        user_roster: list[Player],
        current_pick: int,
        total_picks: int,
        config: LeagueConfig,
    ) -> SnakePickRecommendation:
        """
        Given current draft state, recommend the best pick.
        
        Factors:
        1. Best available by system rank (primary)
        2. Positional need (roster construction)
        3. Positional scarcity (how long can you wait?)
        4. Strategy (Zero RB, Hero RB, etc.)
        """
        round_num = (current_pick - 1) // config.team_count + 1
        
        # Build context for Sonnet
        context = {
            "current_pick": current_pick,
            "round": round_num,
            "picks_remaining": total_picks - current_pick,
            "user_roster": [
                {
                    "name": p.name,
                    "position": p.position,
                    "tier": p.tier,
                }
                for p in user_roster
            ],
            "top_available": [
                {
                    "name": p.name,
                    "position": p.position,
                    "system_rank": r.system_rank,
                    "adp": r.adp_rank,
                    "voe": r.voe,
                    "proj_ppr": r.proj_ppr,
                    "scarcity": r.positional_scarcity,
                    "flags": [
                        f.flag_type for f in p.flags
                    ],
                }
                for p, r in self._get_top_available(
                    available_players, 10
                )
            ],
            "roster_needs": self._assess_needs(
                user_roster, round_num, config
            ),
            "scoring": config.scoring,
            "team_count": config.team_count,
        }
        
        recommendation = await self._call_sonnet(context)
        return recommendation
    
    def _assess_needs(
        self,
        roster: list[Player],
        round_num: int,
        config: LeagueConfig,
    ) -> dict:
        """
        What positions does this roster need?
        Returns urgency: critical | needed | optional | filled
        """
        position_counts = Counter(p.position for p in roster)
        
        needs = {}
        # By round 4, need at least 1 QB
        needs["QB"] = (
            "critical" if round_num >= 8 and position_counts.get("QB", 0) == 0
            else "needed" if round_num >= 5 and position_counts.get("QB", 0) == 0
            else "optional"
        )
        needs["RB"] = (
            "critical" if position_counts.get("RB", 0) < 2
            else "needed" if position_counts.get("RB", 0) < 3
            else "filled"
        )
        needs["WR"] = (
            "critical" if position_counts.get("WR", 0) < 2
            else "needed" if position_counts.get("WR", 0) < 3
            else "filled"
        )
        needs["TE"] = (
            "needed" if position_counts.get("TE", 0) == 0 and round_num >= 4
            else "optional"
        )
        return needs
```

**Sonnet prompt for snake picks:**
```python
SNAKE_DRAFT_PROMPT = """You are an expert fantasy football 
draft advisor for a snake draft.

Current pick: {current_pick} (Round {round})
Your roster so far: {user_roster}
Roster needs: {roster_needs}

Top available players:
{top_available}

Give a pick recommendation. Consider:
1. Best player available by system rank
2. Positional need and urgency
3. Positional scarcity (how long can you wait?)
4. Round context (early: BPA; mid: fill needs; late: upside)

Output JSON:
{
  "recommended_player": "name",
  "reasoning": "2-3 sentences max",
  "backup_option": "name if recommended taken",
  "urgency": "must_take | should_take | can_wait",
  "positions_to_avoid": ["QB"] // if can wait on position
}"""
```

---

## Part 5 — Strategy modes for snake

The 5 strategies (hero_rb, zero_rb, balanced, etc.) apply to snake
but manifest differently:

```python
SNAKE_STRATEGY_WEIGHTS = {
    "hero_rb": {
        # Take elite RB early, fill WR in middle rounds
        "RB": {"rounds_1_3": 1.5, "rounds_4_8": 0.8},
        "WR": {"rounds_1_3": 0.7, "rounds_4_8": 1.3},
    },
    "zero_rb": {
        # Avoid RBs early, stack WRs
        "RB": {"rounds_1_3": 0.3, "rounds_4_8": 1.2},
        "WR": {"rounds_1_3": 1.5, "rounds_4_8": 1.0},
    },
    "balanced": {
        # Take BPA regardless of position
        "RB": {"rounds_1_3": 1.0, "rounds_4_8": 1.0},
        "WR": {"rounds_1_3": 1.0, "rounds_4_8": 1.0},
    },
    # etc.
}
```

Strategy adjusts the system_rank weighting when the agent
evaluates available players. It doesn't override obvious value —
a Tier 1 player at a pick 6 ADP is still a take regardless of
strategy.

---

## Part 6 — Playwright bridge for snake

The existing Playwright bridge watches Yahoo WS frames.
Snake draft frames are different from auction frames.

```python
# backend/integrations/yahoo_playwright.py

class SnakeDraftBridge:
    """
    Playwright bridge for snake draft rooms.
    Watches for draft pick events (not bid events).
    
    Key differences from auction bridge:
    - No bid commands
    - Picks happen automatically on your turn
    - Need to detect: "your pick" notification
    - Need to submit: player selection
    """
    
    async def wait_for_your_pick(self) -> DraftState:
        """
        Monitor WS frames until it's the user's turn.
        Returns current draft state when pick clock starts.
        """
        ...
    
    async def submit_pick(self, player_id: str) -> bool:
        """
        Select a player when it's your turn.
        Returns True if successful.
        """
        ...
    
    async def get_available_players(self) -> list[str]:
        """
        Get list of undrafted player IDs from current
        draft board state.
        """
        ...
```

---

## Required test cases

```python
# Snake valuation
def test_voe_positive_when_system_rank_better_than_adp()
def test_voe_negative_when_reaching()
def test_scarcity_scarce_when_few_players_remain()
def test_snake_tier_elite_for_top_ranked()
def test_snake_tier_value_for_high_voe()

# Snake draft agent
def test_recommendation_json_valid()
def test_critical_need_overrides_bpa()
    """If QB need is critical and good QB available, take it"""
def test_strategy_weights_applied()
def test_backup_option_different_position()

# ADP data
def test_adp_loaded_for_correct_platform()
def test_adp_ppr_different_from_half_ppr()
def test_voe_calculated_correctly()

# UI
test('snake draft board shows ADP column not bid ceiling')
test('VOE badge shows VALUE for positive VOE')
test('recommendation card shows pick reasoning')
test('strategy selector changes position weights')
```

---

## Verification before marking complete

1. Snake draft board shows ADP and VOE columns (not bid ceilings)
2. VOE correctly positive for players available later than ADP
3. Strategy selector changes which positions are highlighted
4. Live agent gives sensible pick recommendation for test scenario:
   - Round 1 pick 4: should recommend top WR/RB available
   - Round 6 with no QB: should flag QB need
   - Round 10: should mention upside/late-round value
5. Roster need assessment correctly identifies gaps
6. **ASK USER** to test with a mock snake draft scenario

---

## Commit
```
feat(saas): snake draft support

SnakeValuationEngine converts PPR projections to ADP rankings.
VOE (Value Over Expected) metric: system_rank vs ADP consensus.
Snake-specific tiers: elite/strong/value/reach/depth.
Positional scarcity assessment by round window.
SnakeDraftAgent gives Sonnet-powered pick recommendations.
Strategy modes adapted for snake (hero_rb, zero_rb, etc.).
Snake draft board UI with ADP/VOE columns.
Playwright bridge extended for snake pick submission.
ADP data from FantasyPros stored per platform + scoring.
Coverage: X%.
```
