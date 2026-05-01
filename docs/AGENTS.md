# Pre-Draft Agent Specifications

Six research agents run before draft day. They run in order — Team Systems first,
others can parallelize after. Each writes structured output to the PostgreSQL draft bible.

All agents must:
- Import season years from `backend.utils.seasons` — never hardcode
- Use `BaseAgent.call_once()` — never `run_agent()`
- Batch by team — one API call per team maximum
- Pre-aggregate all data in Python before the API call
- See `docs/rules/COST_RULES.md` and `docs/rules/PATTERNS.md`

---

## Agent 1: Team Systems

**Model:** `claude-haiku-4-5-20251001` (data extraction)
**Max tokens:** 500 per team
**Total API calls:** 32 (one per NFL team)

**Purpose:** Grade every NFL team as an offensive system. Runs first.
Output inherited by all other agents — do not run other agents until this completes.

**Data inputs (pre-aggregated in Python):**
- O-line pass protection grade (proxy from pressure rate allowed, sack rate)
- O-line run blocking grade (adjusted line yards)
- QB: starter name, experience years, CPOE, air yards per attempt, pressure performance
- OC: name, scheme tendencies from last 3 coaching stops, run/pass split, personnel groupings
- Red zone usage patterns

**Key logic:**
- `rookie_qb_flag`: true for any first-year NFL starter
- `compound_risk_flag`: true when `rookie_qb_flag` AND pass protection grade C or below
  → This flag cascades as a severe system penalty to ALL skill positions on that roster
- Split O-line into pass protection AND run blocking separately — they don't always correlate

**Output per team → `team_systems` table:**
```json
{
  "team_abbr": "LAC",
  "pass_protection_grade": "B+",
  "run_blocking_grade": "B",
  "qb_name": "Justin Herbert",
  "qb_tier": "solid",
  "qb_experience_years": 6,
  "qb_pressure_performance": "above_avg",
  "qb_cpoe": 2.4,
  "qb_air_yards_per_attempt": 8.2,
  "qb_downfield_aggressiveness": "moderate",
  "rookie_qb_flag": false,
  "compound_risk_flag": false,
  "oc_name": "[Name]",
  "oc_scheme": "balanced",
  "oc_run_pass_split_tendency": 0.52,
  "personnel_tendency": "11",
  "red_zone_philosophy": "wr1",
  "system_ceiling": "high",
  "system_grade": "A-",
  "notes": "2-3 sentence summary"
}
```

---

## Agent 2: Roster Changes

**Model:** `claude-sonnet-4-6` (causal reasoning required)
**Max tokens:** 2000 per team
**Total API calls:** 32

**Purpose:** Track every offseason transaction and reason through downstream consequences.
The McConkey/Allen scenario is the canonical test case this agent must catch.

**The McConkey/Allen chain (canonical example):**
```
Event: Keenan Allen signs with LAC
→ Allen's historical role: slot receiver, 27% target share with Herbert
→ McConkey's role: slot receiver, same alignment as Allen
→ Conclusion: direct role overlap → DISPLACED flag on McConkey
→ Both sides flagged: McConkey also gets CONTINGENT flag (value rises if Allen absent)
```

**Dependency flag types:**
- `displaced`: Role directly overlapped by new arrival (negative when trigger is healthy)
- `contingent`: Value tied to trigger player's health (positive modifier when trigger is out)
- `beneficiary`: Clear value increase when trigger is absent
- `committee`: RB sharing backfield, snap share unclear
- `scheme_fit`: Player profile mismatches new OC tendency
- `college_trust`: QB/WR college connection on same NFL roster (positive, especially Year 1)

**Always flag BOTH sides of a displacement:**
Displaced player gets DISPLACED (negative) + CONTINGENT (positive if trigger absent).

**QB Trust Model:**
- Build trust score (0-100) for every QB/receiver pairing
- NFL shared history = primary source
- College shared history = secondary source (~70% weight)
- College flag especially important for rookie QBs in Year 1 — default to college targets under pressure

**Output → `player_dependencies` table (multiple records per player):**
```json
{
  "player_name": "Ladd McConkey",
  "player_team": "LAC",
  "player_position": "WR",
  "flag_type": "displaced",
  "trigger_player_name": "Keenan Allen",
  "trigger_player_team": "LAC",
  "trigger_condition": "active_and_healthy",
  "effect_on_value": "negative",
  "value_impact_pct": -0.35,
  "confidence": "high",
  "reasoning": "Allen commands 27% historical target share with Herbert. Direct slot role overlap.",
  "season_year": 2026
}
```

**Canonical test — must pass before stage is complete:**
```bash
pytest tests/unit/agents/test_roster_changes.py::test_mcconkey_allen_displacement -v
```

---

## Agent 3: Player Profiles

**Model:** `claude-haiku-4-5-20251001`
**Max tokens:** 1000 per team batch
**Total API calls:** 32

**Purpose:** Build a complete individual profile for every draftable player.
Inherits team system context from Agent 1.

**Role classifications:**
WR: `wr1_alpha`, `slot_specialist`, `deep_threat`, `possession_wr2`, `gadget`
RB: `workhorse`, `early_down_thumper`, `pass_catching_specialist`, `committee_back`

**Key metrics (pre-aggregated from nfl_data_py + nflfastR):**
- Target share % (per season and per game)
- Targets per route run
- Air yards share
- Snap percentage and route participation rate
- Separation score (at snap and at catch)
- Yards after catch
- Yards after contact (RBs)
- Broken tackle rate (RBs)

**Clean season baseline:** Strip injury-shortened seasons (<10 games) and
anomalous situations (backup QB for 4+ games). Document excluded seasons.

**Age curve modifiers:**
- RBs: peak 24-26, decline flag after 28
- WRs: peak 24-29
- TEs: peak 26-29 (slow development position)
- Contract year flag: final year of contract → mild upward bias, note it

**Breakout candidate detection:**
- Year 2 or Year 3 spike window
- Clear path to increased target share from depth chart departure
- New OC scheme elevates this player type
- Efficiency already above production level

**Output → `player_profiles` table**

---

## Agent 4: Injury Risk

**Model:** `claude-haiku-4-5-20251001`
**Max tokens:** 1000 per team batch
**Total API calls:** 32

**Purpose:** Risk-adjust every player's value. Not predicting injuries — pricing in variance.

**Injury categories:**
| Category | Recurrence | Key note |
|----------|-----------|----------|
| `soft_tissue` (hamstring, groin, calf) | HIGH | Two same-area events in 3yr = RECURRING_SOFT_TISSUE flag |
| `ligament_acl` | MODERATE | Low re-tear on same knee, elevated contralateral risk |
| `high_ankle_sprain` | MODERATE | Underrated lingering effect |
| `fracture_traumatic` | LOW | Heals cleanly, flag recency only |
| `fracture_stress` | MODERATE | Indicates biomechanical issue |
| `concussion` | SPECIAL | 2+ = compounding modifier |
| `chronic` (turf toe, back, plantar) | ONGOING | Does not reset between seasons |

**Pattern flags:**
`RECURRING_SOFT_TISSUE`, `CONCUSSION_HISTORY`, `HIGH_MILEAGE` (RB 600+ carries),
`POST_ACL` (within 18 months), `CHRONIC_CONDITION`, `WORKLOAD_CLIFF` (RB 300+ carries)

**Age risk multiplier:**
Under 26: 1.0x | 26-28: 1.1x | 29-30: 1.25x | 31+: 1.5x

**Risk-adjusted value modifier** (applied to baseline):
Low: 0 to -5% | Moderate: -10 to -20% | High: -20 to -35% | Volatile: -35%+

**Output → `player_injury_profiles` table**

---

## Agent 5: Schedule

**Model:** `claude-haiku-4-5-20251001`
**Max tokens:** 1500 per team batch (3-position JSON with playoff_matchups arrays requires ~1100-1200 tokens)
**Total API calls:** 32

**Purpose:** Grade each player's schedule across three distinct windows.

**Three windows — store all three separately:**
1. Early (weeks 1-6): Determines fast start. More weight for immediate contributors.
2. Full season: Standard quality-of-schedule.
3. **Playoff (weeks 14-17): Most underrated metric. First-class field, not buried in notes.**

**Defensive grade construction:**
Start from last season's stats. Adjust for:
- FA losses/additions weighted by position relevance
- Draft picks by projected role
- Coordinator changes (scheme impact)
Build separate grades: vs WR1, vs slot WR, vs TE, vs RB rushing, vs RB receiving

**Additional factors:**
- Bye week number
- Weather risk (outdoor cold-weather cities: BUF, GB, CHI, NE, CLE, PIT — Nov/Dec modifier)
- Divisional game weeks (mild suppression)

**Output → `player_schedules` table**

The `playoff_window_grade` must be a queryable column. Never store it only in notes.

---

## Agent 6: Beat Reporter

**Model:** `claude-haiku-4-5-20251001`
**Max tokens:** 300 per signal
**Run schedule:** Daily the week of draft; weekly during season

**Purpose:** Freshness layer. Catch last-mile signals before other agents do.

**Signal types:**
- Practice limitation (Limited/DNP on injury report)
- Depth chart change (official or reported)
- Coach evasive about player status
- Camp standout / emerging role
- Transaction not yet in OverTheCap

**Data sources:**
- Team beat reporter RSS feeds (ESPN, NFL.com, The Athletic)
- Official NFL injury reports (Wed-Fri)
- Rotowire transaction feed

**Output:** Updates `notes` and `last_updated` on player records.
Also writes to `beat_reporter_signals` table with timestamp, source, signal type, raw text.

This agent runs on a daily schedule via APScheduler. It feeds into the
Injury Risk agent's `recovery_assessment` field for recently injured players.
