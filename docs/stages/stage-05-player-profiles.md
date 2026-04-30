# Stage 5: Player Profiles Agent

## Before starting this stage, read:
- `docs/AGENTS.md` — Agent 3: Player Profiles spec
- `docs/rules/COST_RULES.md`
- `docs/rules/PATTERNS.md`

---

## Goal
Every draftable player (top 200 ADP minimum) has a complete profile record
in the `player_profiles` table.

---

## Model and cost parameters
- Model: `claude-haiku-4-5-20251001`
- Max tokens: 1000 per team batch
- Total API calls: 32 (one per NFL team)
- Pattern: pre-aggregate in Python → single Haiku call per team → parse JSON array

---

## Files to create / modify
- `backend/agents/player_profiles.py` (create)
- `tests/unit/agents/test_player_profiles.py` (create)

---

## Season years
All season references must use `backend.utils.seasons`:
```python
from backend.utils.seasons import get_current_season, get_analysis_seasons, get_analysis_year

ANALYSIS_SEASONS = get_analysis_seasons(3)
ANALYSIS_YEAR = get_analysis_year()
```
Never hardcode years. If you write `2024` or `2025` anywhere in this file, that is a bug.

---

## Data to pre-aggregate per team (Python only, no API calls)
For each team:
1. Pull all skill position players from roster
2. For each player, aggregate from nfl_data_py + nflfastR across ANALYSIS_SEASONS:
   - Target share % per season
   - Targets per route run
   - Air yards share
   - Snap percentage
   - Separation score
   - Yards after catch
   - For RBs: yards after contact, broken tackle rate
3. Pull team system grade from DB (already written by Agent 1)
4. Pull dependency flags from DB (already written by Agent 2)
5. Build compact summary dict

Pass entire team's player summaries in one Haiku call.
Model returns JSON array — one profile object per player.

---

## Clean season baseline logic
Strip seasons where:
- Games played < 10 (injury-shortened)
- Team started backup QB for 4+ games in that season

Document excluded seasons in `anomalous_seasons_excluded` array field.
Project baseline from clean seasons only.

---

## Breakout candidate detection
Flag a player as breakout candidate if ANY of:
- Player is in Year 2 or Year 3 (WR/TE) — spike window
- Depth chart departure above them opens target share
- New OC scheme historically elevates this player type
- Efficiency metrics already above what production stats show

---

## Required test cases
```python
def test_clean_season_baseline_strips_injury_year():
def test_clean_season_baseline_strips_backup_qb_year():
def test_breakout_flag_year2_wr():
def test_breakout_flag_depth_chart_departure():
def test_role_classification_wr1_alpha():
def test_role_classification_committee_back():
def test_system_grade_inherited_from_team_systems():
def test_dependency_flags_attached_to_profile():
def test_no_hardcoded_years():  # scans file for literal year integers
def test_single_api_call_per_team():  # verifies one call in run_for_team()
```

---

## Verification before marking complete
1. All 200+ players have profile records
2. Situation scores distribute reasonably (not all "strong")
3. Clean season baselines look accurate for 5 spot-checked players
4. Breakout flags exist on at least a few players
5. No hardcoded years anywhere in the file
6. All required test cases pass
7. Coverage 80%+ on `player_profiles.py`
8. Committed as `feat(player-profiles): implement player profiles agent`

---

## Ask user
- To spot-check 5 player records and confirm they look correct before committing
- Before running the full 32-team pipeline (show dry-run estimate first)
