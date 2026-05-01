# Stage 5: Player Profiles Agent

## Before starting this stage, read:
- `docs/AGENTS.md` — Agent 3: Player Profiles spec
- `docs/rules/COST_RULES.md`
- `docs/rules/PATTERNS.md`

---

## Goal
Every draftable player (top 200 ADP minimum) has a complete profile record
in the `player_profiles` table. Veterans are profiled from NFL history.
Rookies are profiled from college data and historical comps.

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
2. Separate into veterans (nfl_seasons_played > 0) and rookies (is_rookie == True)
3. For veterans, aggregate from nfl_data_py + nflfastR across ANALYSIS_SEASONS:
   - Target share % per season (average across clean seasons — never sum)
   - Targets per route run
   - Air yards share
   - Snap percentage
   - Separation score
   - Yards after catch
   - For RBs: yards after contact, broken tackle rate
4. For rookies, pull from draft bible (already populated by Agent 2):
   - college_profile_grade, comp_yr1_avg_ppg, comp_yr2_avg_ppg
   - draft_capital_signal, landing_spot_modifier
5. Pull team system grade from DB (already written by Agent 1)
6. Pull dependency flags from DB (already written by Agent 2)
7. Build compact summary dict for the full team

Pass entire team's player summaries (veterans + rookies) in one Haiku call.
Model returns JSON array — one profile object per player.

---

## Clean season baseline logic (veterans only)
Strip seasons where:
- Games played < 10 (injury-shortened)
- Team started backup QB for 4+ games in that season

Document excluded seasons in `anomalous_seasons_excluded` array field.

**CRITICAL: Average across clean seasons — never sum them.**
If a player has 3 clean seasons with 1,200 / 1,100 / 1,300 receiving yards,
the baseline is 1,200 yards — not 3,600.
A baseline showing 2,800+ receiving yards is ALWAYS a bug (multi-season sum).

**PPR points formula — use exactly this:**
```python
ppr_points = (receptions * 1.0) + (yards * 0.1) + (touchdowns * 6.0)
```
If stored ppr_points diverges from this formula by more than 5 points,
there is a double-counting bug. Find it and fix it before proceeding.

---

## Breakout candidate detection (veterans)
Flag a player as breakout candidate if ANY of:
- Player is in Year 2 or Year 3 (WR/TE) — spike window
- Depth chart departure above them opens target share
- New OC scheme historically elevates this player type
- Efficiency metrics already above what production stats show

---

## Rookie profiling branch

Rookies have no NFL history. They cannot use the clean season baseline.
The agent must detect rookie status and route to a completely separate path.

```python
def build_player_profile(player: dict, team_context: dict) -> dict:
    if player.get("is_rookie") or player.get("nfl_seasons_played", 0) == 0:
        return _build_rookie_profile(player, team_context)
    return _build_veteran_profile(player, team_context)
```

**Rookie profile construction:**

```python
def _build_rookie_profile(player: dict, team_context: dict) -> dict:
    """
    Rookies are profiled from college data + historical comps.
    All comp data pre-populated by Agent 2 (Roster Changes).

    Sources already in player record from Agent 2:
      player["college_profile_grade"]  — elite/strong/average/weak
      player["comp_yr1_avg_ppg"]       — historical comp Yr1 PPR/game avg
      player["draft_capital_signal"]   — high/medium/low
      player["landing_spot_modifier"]  — from team system grade (0.75-1.18)
    """
    base_ppg = player.get("comp_yr1_avg_ppg") or _get_position_default_ppg(
        player["position"]
    )
    landing_modifier = player.get("landing_spot_modifier", 1.0)
    adjusted_ppg = base_ppg * landing_modifier

    # Per-season projection (17 games)
    projected_ppr_season = adjusted_ppg * 17

    # Confidence discount by position — rookies are inherently high variance
    ROOKIE_CONFIDENCE_DISCOUNT = {
        "QB":  0.65,   # Most QBs take 2-3 years — highest discount
        "WR":  0.75,   # Route running takes time against NFL coverage
        "TE":  0.70,   # Hardest position to translate — takes 3-4 years
        "RB":  0.85,   # Translate fastest — contribute in Year 1
    }
    confidence_discount = ROOKIE_CONFIDENCE_DISCOUNT.get(
        player["position"], 0.75
    )
    discounted_baseline = projected_ppr_season * confidence_discount

    # Rookie ceiling/floor is WIDER than veteran
    # Veterans: ceiling = baseline × 1.25, floor = baseline × 0.75
    # Rookies:  ceiling = baseline × 1.45, floor = baseline × 0.55
    ceiling = discounted_baseline * 1.45
    floor   = discounted_baseline * 0.55

    # Breakout window by position
    DEVELOPMENT_TIMELINE = {
        "QB": "year_2_to_4",
        "WR": "year_2_to_3",
        "TE": "year_3_to_4",
        "RB": "year_1",      # RBs contribute immediately
    }

    # Year 1 role estimate
    year1_role = _estimate_year1_role(player, team_context)

    # High-upside breakout flag
    # Elite college profile + high draft capital = genuine Year 1 upside
    # e.g. Ja'Marr Chase, Justin Jefferson — anomalies but real
    is_breakout_candidate = (
        player.get("college_profile_grade") == "elite"
        and player.get("draft_capital_signal") == "high"
    )

    return {
        "is_rookie": True,
        "profile_source": "college_comps",
        "clean_season_baseline": {
            "ppr_points": round(discounted_baseline, 1),
            "note": "Derived from historical comp average — not NFL history",
        },
        "ceiling_value_ppr": round(ceiling, 1),
        "floor_value_ppr": round(floor, 1),
        "confidence": "low",
        "variance_flag": True,
        "college_profile_grade": player.get("college_profile_grade"),
        "draft_capital_signal": player.get("draft_capital_signal"),
        "historical_comp_names": player.get("historical_comp_names", []),
        "comp_yr1_avg_ppg": player.get("comp_yr1_avg_ppg"),
        "comp_yr2_avg_ppg": player.get("comp_yr2_avg_ppg"),
        "landing_spot_modifier": landing_modifier,
        "breakout_window": DEVELOPMENT_TIMELINE.get(player["position"]),
        "year1_role": year1_role,
        "breakout_candidate": is_breakout_candidate,
        "anomalous_seasons_excluded": [],  # Not applicable for rookies
    }

def _estimate_year1_role(player: dict, team_context: dict) -> str:
    """Estimate likely Year 1 role based on draft capital + depth chart."""
    capital = player.get("draft_capital_signal", "medium")
    depth_rank = player.get("depth_chart_rank", 2)

    if capital == "high" and depth_rank == 1:
        return "starter"
    if capital == "high":
        return "rotational"
    if capital == "medium" and depth_rank <= 2:
        return "rotational"
    return "depth"

def _get_position_default_ppg(position: str) -> float:
    """Fallback PPG if no comp data available."""
    return {"QB": 16.0, "RB": 9.0, "WR": 9.5, "TE": 7.0}.get(position, 8.0)
```

---

## Required test cases
```python
# tests/unit/agents/test_player_profiles.py

# --- Veteran player tests ---
def test_clean_season_baseline_strips_injury_year():
    """Season with < 10 games excluded from baseline."""

def test_clean_season_baseline_strips_backup_qb_year():
    """Season with backup QB for 4+ games excluded from baseline."""

def test_clean_season_baseline_averages_not_sums():
    """
    3 clean seasons of [1200, 1100, 1300] receiving yards
    → baseline is 1200, NOT 3600.
    Any baseline showing 2800+ receiving yards is a bug.
    """

def test_ppr_formula_no_double_counting():
    """
    ppr_points == receptions * 1.0 + yards * 0.1 + touchdowns * 6.0
    Tolerance: ± 5 points. Any larger divergence = double counting bug.
    """

def test_breakout_flag_year2_wr():
    """WR in Year 2 → breakout_candidate = True"""

def test_breakout_flag_depth_chart_departure():
    """WR1 departed → WR2 with cleared path gets breakout_candidate = True"""

def test_role_classification_wr1_alpha():
    """Player with 25%+ target share → role_classification = 'wr1_alpha'"""

def test_role_classification_committee_back():
    """RB with split backfield → role_classification = 'committee_back'"""

def test_system_grade_inherited_from_team_systems():
    """Player profile includes system_grade from team_systems table."""

def test_dependency_flags_attached_to_profile():
    """Player with DISPLACED flag in DB → flag appears in profile output."""

def test_no_hardcoded_years():
    """Scan player_profiles.py source for any integer matching 202[0-9]."""

def test_single_api_call_per_team():
    """run_for_team() makes exactly ONE call to BaseAgent.call_once()."""

# --- Rookie profiling tests ---
def test_rookie_routed_to_rookie_branch():
    """Player with is_rookie=True uses _build_rookie_profile, not veteran path."""

def test_veteran_not_routed_to_rookie_branch():
    """Player with is_rookie=False uses _build_veteran_profile."""

def test_rookie_profile_uses_comp_data_not_nfl_history():
    """Rookie baseline derived from comp_yr1_avg_ppg × confidence_discount."""

def test_rookie_confidence_discount_qb_is_lowest():
    """QB rookie discount (0.65) < WR (0.75) < TE (0.70) < RB (0.85)"""

def test_rookie_confidence_discount_rb_is_highest():
    """RB discount is 0.85 — translates fastest from college."""

def test_rookie_wider_ceiling_floor_range():
    """
    Rookie: ceiling = baseline × 1.45, floor = baseline × 0.55
    Veteran: ceiling = baseline × 1.25, floor = baseline × 0.75
    Rookie range must be wider.
    """

def test_rookie_variance_flag_always_true():
    """All rookies have variance_flag=True regardless of college profile grade."""

def test_rb_rookie_development_timeline_year1():
    """RB rookies → breakout_window = 'year_1'"""

def test_wr_rookie_development_timeline_year2_3():
    """WR rookies → breakout_window = 'year_2_to_3'"""

def test_te_rookie_development_timeline_year3_4():
    """TE rookies → breakout_window = 'year_3_to_4'"""

def test_elite_profile_high_capital_is_breakout_candidate():
    """
    college_profile_grade='elite' AND draft_capital_signal='high'
    → breakout_candidate = True even as a rookie.
    This is the Ja'Marr Chase / Justin Jefferson flag.
    """

def test_landing_spot_modifier_applied_to_projection():
    """
    Rookie with comp_yr1_avg_ppg=12.0 and landing_modifier=0.75
    → adjusted baseline < 12.0 × 17 games
    """

def test_average_profile_low_capital_not_breakout_candidate():
    """college_profile_grade='average', capital='low' → breakout_candidate=False"""
```

---

## Verification before marking complete
1. All 200+ players have profile records
2. Situation scores distribute reasonably (not everyone is "strong")
3. **Veteran baselines look correct** — spot-check Barkley, Jefferson, Bowers
   - Barkley yards should be ~1,400-1,700 (single season avg), NOT 2,800+
   - Jefferson ppr_points should be ~195-220, reconciles with PPR formula
4. Rookies have `is_rookie=True`, `variance_flag=True`, `profile_source='college_comps'`
5. **ASK USER** to review 2-3 rookie profiles — do comps and projections look reasonable?
6. Breakout flags exist on at least a few players (veterans and rookies)
7. No hardcoded years anywhere in `player_profiles.py`
8. All 12 veteran tests AND 12 rookie tests pass
9. Coverage 80%+ on `player_profiles.py`

---

## Commit
```
feat(player-profiles): implement Player Profiles Agent with rookie branch

Veteran profiles from NFL history (averaged clean seasons).
PPR formula verified — no double counting.
Rookie profiles from college comps and landing spot modifier.
Position-specific confidence discounts (QB 65%, WR 75%, TE 70%, RB 85%).
Wider ceiling/floor range for rookies vs veterans.
Development timeline flags by position.
Elite profile + high capital = breakout candidate flag.
All 24 named tests passing (12 veteran + 12 rookie).
Coverage: X%.
```

---

## Ask user
- To spot-check 5 veteran player records before committing (especially Barkley and Jefferson)
- To spot-check 2-3 rookie records — do the comps and projections look reasonable?
- Before running the full 32-team pipeline (show dry-run estimate first)
