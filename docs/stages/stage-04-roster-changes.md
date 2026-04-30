# Stage 4: Roster Changes Agent

## Before starting, read:
- `docs/AGENTS.md` — Agent 2: Roster Changes spec
- `docs/rules/COST_RULES.md`
- `docs/rules/PATTERNS.md`

---

## Goal
Every meaningful offseason transaction analyzed with dependency flags written to DB.
The McConkey/Allen scenario must be caught. This is the most complex reasoning agent.

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

Never pass raw play-by-play data. Pre-aggregate to summaries only.

### 5. Data caching
Pre-warm `_DATA_CACHE` in `run_all_teams()` BEFORE concurrent team runs:
```python
for season in get_analysis_seasons():
    _set_cached_data(f"target_share_{season}", nfl_data.compute_target_share(season))
    _set_cached_data(f"weekly_stats_{season}", nfl_data.fetch_weekly_stats(season))
```
Never reload season data inside team loops.

### 6. Bulk DB writes
Use `_bulk_resolve_player_ids()` — one DB query for all names, not one per flag.
Use bulk insert for all flags from one team in a single transaction.

---

## Required test cases
```python
# tests/unit/agents/test_roster_changes.py

def test_mcconkey_allen_displacement()
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
2. All 11 named tests pass
3. Run for all 32 teams — consistent flag counts (not 1-2 per team)
4. **ASK USER** to review dependency flags for 3 teams they know well
5. Verify DISPLACED flags always have matching CONTINGENT flags
6. No hardcoded years anywhere in `roster_changes.py`
7. Coverage 80%+

---

## Commit
```
feat(roster-changes): implement Roster Changes Agent

Chain-of-reasoning dependency flag generation.
McConkey/Allen canonical test passing.
All 11 named test cases passing.
DISPLACED/CONTINGENT always generated as pairs.
Single Sonnet call per team, bulk DB writes.
Coverage: X%.
```

---

## Ask user
- To review dry-run estimate before running (expected ~$0.40 for all 32 teams)
- To review dependency flags for teams with known offseason changes
