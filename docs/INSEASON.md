# In-Season Features

All in-season features build on the draft bible. They extend it — they do not replace it.
The season roster store starts as the draft bible records for your drafted players,
then gets extended with weekly tracking fields.

---

## Season Roster Store

Populated immediately after draft completes via Yahoo API draft results sync.

Extends player records with:
- `acquisition_price` — what was paid at auction
- `weekly_stats` — JSONB array, one entry per week
- `weekly_snap_counts` — JSONB array
- `weekly_target_share` — JSONB array
- `current_trade_value` — updated weekly
- `value_trend` — rising / falling / stable
- `sell_high_flag`, `buy_low_flag`, `injury_concern_flag`

---

## Roster Monitor Agent

**Runs:** Wednesday weekly (after MNF stats finalize)
**Model:** `claude-haiku-4-5-20251001`

Tasks:
- Pull weekly stats from Yahoo API for all rostered players
- Update snap count and target share trend arrays
- Detect usage drops (2+ consecutive weeks declining snaps = flag)
- Pull Wednesday injury report practice participation
- Update `current_trade_value`, `value_trend`
- Set `sell_high_flag` if recent performance exceeds underlying efficiency
- Set `buy_low_flag` if recent slump is matchup-driven

---

## Opponent Analyzer Agent

**Runs:** Wednesday weekly
**Model:** `claude-sonnet-4-6`

Builds and maintains per-opponent profiles throughout the season.

Per-opponent profile:
- Current roster with acquisition prices and weekly scores
- Positional strength scores (updated weekly)
- Roster vulnerabilities (bye conflicts, injury exposure, playoff schedule issues)
- Apparent management style: reactive / analytical / name-brand biased / urgency-driven
- Historical trade behavior: accepted/rejected this season
- Current record and playoff positioning

**Management style detection:**
- Reactive: starts players coming off big games regardless of matchup
- Name-brand biased: holds big names well past their value
- Analytical: trade offers show schedule and usage awareness
- Urgency-driven: losing streak = willing to overpay

---

## Trade Value Agent

**Runs:** Wednesday weekly
**Model:** `claude-haiku-4-5-20251001`

Calculates current trade value for every player in the league (not just your roster).

Buy-low signals:
- Recent slump confirmed as matchup-driven
- Snap count temporarily reduced (non-recurring game script)
- Returning from injury (conservative snaps, will recover)
- Recency bias suppressing value below true projection

Sell-high signals:
- TD production on low target share (TDs regress toward targets)
- Snap count quietly declining 2+ weeks
- Upcoming brutal schedule opponent hasn't noticed
- Overperforming efficiency metrics

Valuation asymmetry: for each opponent, compare your valuation of their players
vs what they likely value them at (using management style profile). Gap = trade opportunity.

---

## Trade Proposal Engine

**On-demand + weekly suggestions**
**Model:** `claude-sonnet-4-6`
**Max tokens:** 2000

Generates proactive trade suggestions by cross-referencing:
- Your roster surplus positions
- Each opponent's roster weakness
- Trade Value Agent's asymmetry flags
- Opponent management style (calibrates framing)
- Acceptance probability model

**Acceptance probability inputs** (in order of weight):
1. Does it address their positional need?
2. Does it look fair at market value?
3. Does it match their management style preferences?
4. Their current record and urgency level
5. Their playoff schedule (creates urgency if bad)

Output: ranked list with acceptance probability and reasoning per proposal.

---

## Trade Analyzer

**On-demand — user submits a trade**
**Model:** `claude-sonnet-4-6`
**Max tokens:** 1500

Endpoint: `POST /trades/analyze`
Input: `{ give: [player_id, ...], receive: [player_id, ...], opponent_team_id }`

Analysis components:
1. **Fairness** — current system value both sides, adjusted for your roster context
2. **Timing flags** — sell-high and buy-low signals on both sides
3. **Acceptance probability** — from opponent profile
4. **Counter proposals** — if unfavorable, find nearest adjustment that flips it

Output format:
```json
{
  "verdict": "slightly_unfavorable",
  "fairness_gap": -12,
  "roster_fit_adjustment": "+4 (receiving at position of need)",
  "timing_flags": [
    "SELL_HIGH on [received player] — 3 TDs on sub-15% target share",
    "BUY_LOW on [given player] — matchup slump, 3 favorable weeks ahead"
  ],
  "acceptance_probability": 0.65,
  "acceptance_reasoning": "Opponent is 3-4, thin at your surplus position",
  "counter_proposals": [
    {
      "description": "Swap [B] for [D]",
      "new_verdict": "favorable",
      "acceptance_probability": 0.58,
      "reasoning": "..."
    }
  ]
}
```

---

## Lineup Optimizer

**Runs:** Thursday weekly (after injury reports + Vegas lines set)
**Model:** `claude-haiku-4-5-20251001`
**Max tokens:** 1000

Inputs per player:
- Matchup grade (this week's specific opponent)
- Vegas implied team total (most predictive single input)
- Practice participation (Wed/Thu/Fri reports)
- Recent snap count and usage trend
- Weather (outdoor stadiums, wind >15mph suppresses passing)
- Home/away split if significant

Output: Starting lineup with confidence level (High/Medium/Low) and key reasoning
per decision. Always explain — user must be able to override with context the system lacks.

Endpoint: `GET /lineup/week/{week_number}`

---

## Waiver Wire Agent

**Runs:** Tuesday night / Wednesday morning (after waiver priority processes)
**Model:** `claude-haiku-4-5-20251001`
**Max tokens:** 800

Identification criteria:
- Snap count spike in most recent game
- Depth chart promotion due to injury above them
- Usage pattern change (target share jump, carry spike)
- Beat reporter signal about increased role
- Favorable schedule next 2-3 weeks

Output: Ranked pickup recommendations with:
- Projected value window (how many weeks this is relevant)
- Confidence level
- Reasoning

Endpoint: `GET /waivers/week/{week_number}`

---

## Pipeline Admin UI

React page in the app for managing the pre-draft pipeline.

Components:
- Pipeline status dashboard (last run time per agent, data freshness)
- Manual trigger buttons per agent
- Data freshness indicators on player cards
- Cost report (from `api_usage_log`)

Endpoint: `GET /admin/pipeline-status`
Endpoint: `POST /admin/pipeline/run` (triggers specific agent)
Endpoint: `GET /admin/cost-report`
