# Stage 4: Roster Changes Agent

## Before starting, read:
- `docs/AGENTS.md` — Agent 2: Roster Changes spec
- `docs/rules/COST_RULES.md`
- `docs/rules/PATTERNS.md`

---

## Goal
Every meaningful offseason transaction analyzed with dependency flags written to DB.
The McConkey/Allen scenario must be caught. NFL draft picks must trigger full
prospect evaluations, not just be recorded as transactions.

---

## Model and cost parameters
- Model: `claude-sonnet-4-6` — causal reasoning required (the ONLY pre-draft agent using Sonnet)
- Max tokens: 2000 per team
- Total API calls: 32 (one per team)
- Pattern: pre-aggregate in Python → single Sonnet call → parse JSON array → bulk DB write

---

## Why Sonnet here and Haiku everywhere else
This agent must reason through chains of cause and effect:
"Allen signs → Herbert's historical usage → McConkey's role overlap → ceiling capped"
That multi-step causal inference requires Sonnet. Data lookup does not.

---

## Tasks

### 1. Dependency flag types
Implement all six flag types:
- `displaced` — role directly overlapped by new arrival
- `contingent` — value tied to trigger player's health (always paired with displaced)
- `beneficiary` — value rises when trigger player is absent
- `committee` — RB sharing backfield
- `scheme_fit` — player profile mismatches new OC tendency
- `college_trust` — QB/WR college connection on same roster

### 2. Always flag BOTH sides of displacement
When McConkey is DISPLACED by Allen:
- McConkey gets DISPLACED flag (negative, trigger=Allen, condition=active_and_healthy)
- McConkey ALSO gets CONTINGENT flag (positive, trigger=Allen, condition=injured_or_absent)
This must be automatic — never flag one without the other.

### 3. QB Trust Model
Build trust score (0-100) for every QB/receiver pairing:
- NFL shared history: primary source — years together, avg target share in shared games
- College shared history: ~70% weight — from cfbfastR data
- College flag especially important for rookie QBs Year 1
- High trust score = ceiling modifier for that receiver

### 4. Pre-aggregation requirements
`_build_team_context()` must produce a compact dict containing:
- All transactions for the team (from OverTheCap, summarized)
- Current skill position roster
- Target share history for all players (last 3 seasons, from cache)
- Backfield usage breakdown
- QB/receiver shared history (NFL + college)
- Team system grade (from team_systems table — Agent 1 must have run first)
- College profiles for all draft picks this year (from cfb_data)
- Historical comp table (pre-loaded once, shared across all teams)

Never pass raw play-by-play data. Pre-aggregate to summaries only.

### 5. Data caching
Pre-warm `_DATA_CACHE` in `run_all_teams()` BEFORE concurrent team runs:
```python
for season in get_analysis_seasons():
    _set_cached_data(f"target_share_{season}", nfl_data.compute_target_share(season))
    _set_cached_data(f"weekly_stats_{season}", nfl_data.fetch_weekly_stats(season))

# Pre-load comp table once — used by all teams with draft picks
_set_cached_data("historical_comp_table", cfb_data.build_historical_comp_table())
```
Never reload season data or the comp table inside team loops.

### 6. Bulk DB writes
Use `_bulk_resolve_player_ids()` — one DB query for all names, not one per flag.
Use bulk insert for all flags from one team in a single transaction.

### 7. NFL draft picks — special transaction category

Draft picks are a fundamentally different transaction than free agent signings.
When the agent encounters `transaction_type == "draft"`, it must trigger a
full prospect evaluation rather than just recording the transaction.

**Why this matters:** A first-round WR is worth completely different values
depending on their college production, draft slot, and landing spot. A 32nd
pick WR with a 40% SEC dominator rating landing behind an elite offensive
line is a completely different asset than a 32nd pick WR with a 22% MAC
dominator rating on a team with a compound risk flag.

**The prospect evaluation pipeline:**

```python
async def _handle_draft_pick(
    self,
    pick: dict,
    team_context: dict,
    comp_table: pd.DataFrame,
) -> list[dict]:
    """
    Full prospect evaluation for NFL draft picks.
    Returns dependency flags AND writes rookie evaluation
    fields to the player's draft bible record.
    """
    player_name = pick["player_name"]
    position = pick["position"]

    # Step 1: Pull college profile (pre-aggregated, no API call)
    college_profile = self._get_college_profile(player_name, position)

    # Step 2: Draft capital
    capital_value = nfl_data.get_draft_capital_value(
        pick["round"], pick["pick_number"]
    )
    capital_signal = nfl_data.get_capital_signal(capital_value)

    # Step 3: Apply conference multiplier to dominator rating
    raw_dominator = college_profile.get("dominator_rating", 0.0)
    conference = college_profile.get("conference", "Unknown")
    adjusted_dominator = cfb_data.get_adjusted_dominator(
        raw_dominator, conference
    )

    # Step 4: Find historical comps (3-5 most similar prospects)
    comps = self._find_historical_comps(
        comp_table=comp_table,
        position=position,
        adjusted_dominator=adjusted_dominator,
        capital_value=capital_value,
        age_at_draft=pick.get("age_at_draft", 22),
    )

    # Step 5: Landing spot modifier from team system grade
    landing_modifier = self._get_landing_spot_modifier(team_context)

    # Step 6: Grade the college profile
    profile_grade = self._grade_college_profile(
        adjusted_dominator=adjusted_dominator,
        yards_per_route=college_profile.get("yards_per_route_run", 0.0),
        position=position,
    )

    # Step 7: Write rookie evaluation fields to player record
    yr1_ppg = (
        sum(c["yr1_ppg"] for c in comps) / len(comps) if comps else None
    )
    yr2_ppg = (
        sum(c["yr2_ppg"] for c in comps) / len(comps) if comps else None
    )

    await self._write_rookie_evaluation({
        "player_name": player_name,
        "is_rookie": True,
        "college_profile_grade": profile_grade,
        "draft_capital_signal": capital_signal,
        "draft_capital_value": capital_value,
        "adjusted_dominator_rating": adjusted_dominator,
        "conference": conference,
        "historical_comp_names": [c["name"] for c in comps[:3]],
        "comp_yr1_avg_ppg": yr1_ppg,
        "comp_yr2_avg_ppg": yr2_ppg,
        "landing_spot_modifier": landing_modifier,
        "projection_confidence": "low",
        "variance_flag": True,
    })

    # Step 8: Generate displacement flags for incumbents
    return await self._generate_rookie_displacement_flags(
        pick, position, capital_signal, team_context
    )
```

**Landing spot modifier scale:**
```python
def _get_landing_spot_modifier(self, team_context: dict) -> float:
    """
    How much does the landing spot help or hurt this rookie's Year 1 outlook?
    Applied multiplicatively to comp-derived projection.
    """
    compound_risk = team_context.get("compound_risk_flag", False)
    rookie_qb    = team_context.get("rookie_qb_flag", False)
    system_grade = team_context.get("system_grade", "C")

    if compound_risk:
        return 0.75   # Rookie QB + bad line = very bad for skill position rookies

    if rookie_qb:
        return 0.85   # Rookie QB alone is a meaningful headwind

    grade_modifiers = {
        "A": 1.18, "B": 1.08, "C": 1.00, "D": 0.88, "F": 0.78
    }
    return grade_modifiers.get(system_grade[0].upper(), 1.00)
```

**College profile grading:**
```python
def _grade_college_profile(
    self,
    adjusted_dominator: float,
    yards_per_route: float,
    position: str,
) -> str:
    """
    Grade based on adjusted dominator rating and yards per route run.
    Conference multiplier already applied to adjusted_dominator.
    """
    if position == "RB":
        # RBs: use usage_rate and yards_after_contact instead
        # Grade handled separately in _grade_rb_profile()
        return self._grade_rb_profile(adjusted_dominator, yards_per_route)

    # WR / TE
    if adjusted_dominator >= 0.38 and yards_per_route >= 2.8:
        return "elite"    # Ja'Marr Chase, Justin Jefferson tier
    if adjusted_dominator >= 0.30 or yards_per_route >= 2.5:
        return "strong"   # Clear NFL starter profile
    if adjusted_dominator >= 0.22:
        return "average"  # Developmental, needs everything to break right
    return "weak"         # Significant doubt about NFL translation
```

**Displacement flags for draft picks:**

A highly-drafted skill position player almost always displaces an incumbent.
Apply the same logic as any other arrival:

```python
async def _generate_rookie_displacement_flags(
    self,
    pick: dict,
    position: str,
    capital_signal: str,
    team_context: dict,
) -> list[dict]:
    """
    First-round picks almost always displace incumbents.
    Third-round+ picks may compete or become depth.
    """
    flags = []

    # Only generate displacement for meaningful picks
    if capital_signal == "low":
        return flags  # 5th-7th round picks rarely displace starters

    incumbents = [
        p for p in team_context.get("current_roster", [])
        if p["position"] == position
        and p["name"] != pick["player_name"]
    ]

    for incumbent in incumbents:
        if capital_signal == "high":
            # First/second round pick → strong displacement signal
            flags.append({
                "player_name": incumbent["name"],
                "flag_type": "displaced",
                "trigger_player_name": pick["player_name"],
                "trigger_condition": "active_and_healthy",
                "effect_on_value": "negative",
                "value_impact_pct": -0.25,
                "confidence": "medium",
                "reasoning": (
                    f"{pick['player_name']} drafted in round {pick['round']} "
                    f"({capital_signal} capital). Will compete for starting role."
                ),
            })
            # Always pair with contingent
            flags.append({
                "player_name": incumbent["name"],
                "flag_type": "contingent",
                "trigger_player_name": pick["player_name"],
                "trigger_condition": "injured_or_absent",
                "effect_on_value": "positive",
                "value_impact_pct": 0.20,
                "confidence": "medium",
                "reasoning": (
                    f"{incumbent['name']} value recovers if "
                    f"{pick['player_name']} misses time."
                ),
            })

    return flags
```

---

## Required test cases
```python
# tests/unit/agents/test_roster_changes.py

def test_mcconkey_allen_displacement():
    """
    THE canonical test.
    Allen signs with LAC.
    McConkey receives DISPLACED flag (negative, trigger=Allen, condition=active_and_healthy).
    McConkey receives CONTINGENT flag (positive, trigger=Allen, condition=injured_or_absent).
    Both flags must be present.
    """

def test_target_share_displacement_direct_role_overlap()
def test_target_share_displacement_no_flag_different_role()
def test_qb_trust_score_nfl_history_high()
def test_qb_trust_score_college_history()
def test_qb_trust_score_no_history_low()
def test_backfield_committee_two_similar_profiles()
def test_backfield_committee_complementary_no_strong_flag()
def test_displaced_always_paired_with_contingent()
def test_high_aav_signing_weighted_higher_than_low_aav()
def test_no_hardcoded_years()
def test_single_api_call_per_team()
def test_data_cache_used_not_reloaded()
def test_bulk_db_write_single_transaction_per_team()

# --- Rookie / draft pick tests ---

def test_draft_pick_triggers_prospect_evaluation():
    """transaction_type='draft' → _handle_draft_pick() called, not generic handler"""

def test_draft_capital_value_round1_is_high():
    """Round 1 pick → capital_signal == 'high'"""

def test_draft_capital_value_round6_is_low():
    """Round 6 pick → capital_signal == 'low'"""

def test_college_dominator_adjusted_for_conference():
    """SEC player dominator unchanged. MAC player dominator × 0.80."""

def test_high_capital_rookie_displaces_incumbent():
    """First-round WR drafted → incumbent WR at same alignment gets DISPLACED flag."""

def test_high_capital_displacement_always_paired_with_contingent():
    """Rookie DISPLACED flag always has matching CONTINGENT flag."""

def test_low_capital_pick_no_displacement():
    """6th round pick → no displacement flags generated."""

def test_landing_spot_compound_risk_modifier():
    """compound_risk_flag=True → landing_modifier == 0.75"""

def test_landing_spot_strong_system_modifier():
    """A-grade system → landing_modifier == 1.18"""

def test_landing_spot_rookie_qb_modifier():
    """rookie_qb_flag=True, no compound risk → landing_modifier == 0.85"""

def test_historical_comps_returned_for_elite_profile():
    """Elite college profile (dominator > 0.38) → at least 1 comp returned"""

def test_rookie_evaluation_fields_written_to_player_record():
    """After handling draft pick → is_rookie, variance_flag, comp fields all set"""
```

---

## Canonical test — must pass before stage is complete
```bash
pytest tests/unit/agents/test_roster_changes.py::test_mcconkey_allen_displacement -v
```
If this test fails, the stage is not complete regardless of anything else.

---

## Verification before marking complete
1. Canonical McConkey/Allen test passes
2. All 13 original dependency flag tests pass
3. All 12 rookie evaluation tests pass
4. Run for all 32 teams — consistent flag counts (not 1-2 per team)
5. **ASK USER** to review dependency flags for 3 teams they know well
6. **ASK USER** to review rookie evaluations for 2-3 known draft picks — do comps make sense?
7. Verify DISPLACED flags always have matching CONTINGENT flags
8. Verify high-capital draft picks generate displacement flags for incumbents
9. No hardcoded years anywhere in `roster_changes.py`
10. Coverage 80%+

---

## Commit
```
feat(roster-changes): implement Roster Changes Agent

Chain-of-reasoning dependency flag generation.
McConkey/Allen canonical test passing.
All 25 named test cases passing (13 dependency + 12 rookie).
NFL draft picks evaluated as prospects with college comps.
Conference-adjusted dominator rating applied.
Landing spot modifier scales from 0.75 (compound risk) to 1.18 (elite system).
DISPLACED/CONTINGENT always generated as pairs.
Single Sonnet call per team, bulk DB writes.
Coverage: X%.
```

---

## Ask user
- To review dry-run estimate before running (expected ~$0.40 for all 32 teams)
- To review dependency flags for teams with known offseason changes
- To review rookie evaluations for 2-3 known draft picks
