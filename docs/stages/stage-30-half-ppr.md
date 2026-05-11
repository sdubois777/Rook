# Stage 30: Half PPR Support & League Config Completion

## Before starting, read:
- `docs/stages/stage-25-saas-foundation.md` (must be complete)
- `docs/LEAGUE_RULES.md`

---

## Goal
Full half PPR scoring support and final league config
parameterization pass. After this stage, the system correctly
handles any standard PPR or half PPR league from 8-14 teams
in either auction or snake format.

---

## Why half PPR matters

Half PPR is the default format on ESPN and extremely common
on Sleeper. It meaningfully changes player values:

- RBs become more valuable relative to WRs
  (RBs have more carries, fewer receptions — half PPR hurts WRs less)
- High-reception WRs/TEs (slot receivers, pass-catching backs)
  become slightly less valuable
- Pure runners (Derrick Henry type) become more valuable
- Workhorse RBs with 200+ carries gain relative to
  pass-catching specialists

The projection math changes but the reasoning layer doesn't.
Sonnet's analysis stays identical — only the PPR formula and
replacement level calculations change.

---

## Part 1 — Update all PPR calculations

### Core formula

Already implemented in Stage 25:
```python
def compute_ppr_points(
    receptions: float,
    yards: float,
    touchdowns: float,
    scoring: str = "ppr",
) -> float:
    REC_MULTIPLIERS = {"ppr": 1.0, "half_ppr": 0.5}
    rec_pts = REC_MULTIPLIERS.get(scoring, 1.0)
    return receptions * rec_pts + yards * 0.1 + touchdowns * 6.0
```

### Audit all callers

Find every place PPR points are computed and verify
`scoring` is being passed through:

```bash
grep -rn "ppr_points\|fantasy_points_ppr\|compute_ppr" \
  backend/ | grep -v test | grep -v ".pyc"
```

Every instance must use `config.scoring` not hardcoded "ppr".

### Historical baseline recomputation

When a user has a half PPR league, the player baselines
need to be recomputed with half PPR scoring.

The Player Profiles agent already stores raw stats
(receptions, yards, touchdowns). The PPR calculation
is applied at valuation time — so passing `config.scoring`
to the valuation engine is sufficient. No profile regeneration
needed.

```python
# In valuation.py — already parameterized after Stage 25
def _extract_ppr(
    profile: PlayerProfile,
    config: LeagueConfig,
) -> float:
    """
    Extract projected PPR from profile using
    the league's scoring format.
    """
    baseline = profile.clean_season_baseline or {}
    
    # Use stored projection if available
    if proj := baseline.get("projected_ppr_season"):
        # Stored projection is PPR — convert if needed
        if config.scoring == "half_ppr":
            # Estimate half PPR from PPR and receptions
            receptions = baseline.get("receptions", 0)
            if receptions:
                ppr_adjustment = receptions * 0.5
                # Half PPR = PPR - (0.5 * receptions)
                return float(proj) - ppr_adjustment
        return float(proj)
    
    # Recompute from raw stats
    return compute_ppr_points(
        receptions=float(baseline.get("receptions", 0)),
        yards=float(baseline.get("yards", 0)),
        touchdowns=float(baseline.get("touchdowns", 0)),
        scoring=config.scoring,
    )
```

---

## Part 2 — Half PPR replacement levels

Replacement levels shift slightly for half PPR.
High-reception players (WRs, pass-catching RBs, TEs)
have slightly lower values relative to rushers.

```python
# In LeagueConfig (Stage 25 already handles this
# by deriving from team_count — but add scoring adjustment)

REPLACEMENT_LEVEL_PPG = {
    "ppr": {
        "QB":  18.0,  # 306 PPR / 17 games
        "RB":   8.0,  # 136 PPR / 17 games
        "WR":   7.0,  # 119 PPR / 17 games
        "TE":   5.0,  #  85 PPR / 17 games
    },
    "half_ppr": {
        "QB":  18.0,  # QBs don't catch passes
        "RB":   7.5,  # Slight decrease (fewer rec pts)
        "WR":   6.5,  # Decrease (less rec value)
        "TE":   4.5,  # Decrease
    },
}

def get_replacement_ppg(
    position: str,
    scoring: str = "ppr",
) -> float:
    return REPLACEMENT_LEVEL_PPG.get(
        scoring, REPLACEMENT_LEVEL_PPG["ppr"]
    ).get(position, 7.0)
```

---

## Part 3 — Valuation agent context update

For half PPR leagues, the Sonnet valuation prompt
needs to know the scoring format so auction notes
reflect the right context:

```python
# In valuation_agent.py, add to player context:
context = {
    ...existing fields...,
    "scoring_format": config.scoring,
    "scoring_note": (
        "This is a HALF PPR league. "
        "Reception bonuses are 0.5 pts not 1.0. "
        "High-volume receivers are worth slightly less "
        "than in PPR. Workhorse RBs with low reception "
        "counts are worth relatively more."
        if config.scoring == "half_ppr"
        else "This is a full PPR league."
    ),
}
```

---

## Part 4 — Draft board UI update for half PPR

The draft board should indicate the scoring format:

```javascript
// In DraftBoard header
<div className="scoring-badge">
  {config.scoring === 'half_ppr'
    ? '½ PPR'
    : 'PPR'
  }
</div>

// Tooltip on player cards showing scoring-adjusted value
<Tooltip content={`${config.scoring === 'half_ppr'
  ? 'Half PPR value'
  : 'Full PPR value'}: ${player.proj_ppr} pts`}
>
```

---

## Part 5 — League config validation

Final pass to ensure LeagueConfig is validated properly
before being used anywhere:

```python
# backend/models/league_config.py

def validate_league_config(config: LeagueConfig) -> None:
    """
    Raise ValueError if config is invalid.
    Called when user creates/updates a league.
    """
    if not 8 <= config.team_count <= 14:
        raise ValueError(
            f"team_count must be 8-14, got {config.team_count}"
        )
    
    if config.draft_type not in ("auction", "snake"):
        raise ValueError(
            f"draft_type must be auction or snake"
        )
    
    if config.scoring not in ("ppr", "half_ppr"):
        raise ValueError(
            f"scoring must be ppr or half_ppr"
        )
    
    if config.draft_type == "auction":
        if not config.budget:
            raise ValueError("auction leagues require budget")
        if not 50 <= config.budget <= 500:
            raise ValueError(
                f"budget must be 50-500, got {config.budget}"
            )
    
    if config.draft_type == "snake":
        if config.pick_position is not None:
            if not 1 <= config.pick_position <= config.team_count:
                raise ValueError(
                    f"pick_position must be 1-{config.team_count}"
                )
```

---

## Part 6 — Comprehensive end-to-end test

Run a full valuation pass for each supported configuration
and verify the outputs make sense:

```python
# tests/integration/test_league_configs.py

CONFIGS_TO_TEST = [
    # Standard 12-team PPR auction (existing behavior)
    LeagueConfig(team_count=12, scoring="ppr", draft_type="auction", budget=200),
    
    # 10-team PPR auction
    LeagueConfig(team_count=10, scoring="ppr", draft_type="auction", budget=200),
    
    # 8-team PPR auction  
    LeagueConfig(team_count=8, scoring="ppr", draft_type="auction", budget=200),
    
    # 14-team PPR auction
    LeagueConfig(team_count=14, scoring="ppr", draft_type="auction", budget=200),
    
    # 12-team half PPR auction
    LeagueConfig(team_count=12, scoring="half_ppr", draft_type="auction", budget=200),
    
    # 12-team PPR snake
    LeagueConfig(team_count=12, scoring="ppr", draft_type="snake"),
    
    # 10-team half PPR snake (most common ESPN format)
    LeagueConfig(team_count=10, scoring="half_ppr", draft_type="snake"),
]

def test_all_configs_produce_valid_valuations(config):
    """
    For each config:
    - Top WR has positive system value
    - Replacement level players have ~$1 value (auction)
      or low system rank (snake)
    - Total auction dollars ≈ config.total_skill_pool
    - No negative values
    - Tier 1 players have higher values than Tier 4
    """

def test_half_ppr_reduces_wr_value_relative_to_rb(config):
    """
    For same player pool:
    half_ppr WR values < ppr WR values
    half_ppr RB values > ppr RB values (relatively)
    """

def test_8_team_has_higher_replacement_than_14_team():
    """
    8-team replacement WR rank = 21 (not 48 for 12-team)
    Fewer teams = less depth needed = higher floor
    """

def test_total_pool_scales_with_team_count():
    """
    8-team total_skill_pool = $200 * 8 * 0.925 = $1,480
    12-team total_skill_pool = $200 * 12 * 0.925 = $2,220
    14-team total_skill_pool = $200 * 14 * 0.925 = $2,590
    """
```

---

## Part 7 — User-facing scoring format display

Anywhere the system discusses player value, show
the scoring context clearly:

```javascript
// PlayerDetailPanel — show scoring format
<div className="scoring-context text-xs text-gray-400">
  Values shown for {config.scoring === 'half_ppr' ? 'Half PPR' : 'PPR'} scoring
</div>

// Dashboard — league card
<div className="league-badge">
  {league.team_count} teams · {league.scoring.toUpperCase()} ·{' '}
  {league.draft_type === 'auction' ? `$${league.budget}` : 'Snake'}
</div>
```

---

## Required test cases

```python
# Scoring formula
def test_ppr_scoring_1_point_per_reception()
def test_half_ppr_scoring_0_5_points_per_reception()
def test_same_rush_yards_same_points_both_formats()
def test_henry_worth_more_in_half_ppr_relative_to_high_rec_wr()

# Replacement levels
def test_half_ppr_wr_replacement_lower_than_ppr()
def test_rb_replacement_decreases_slightly_in_half_ppr()
def test_qb_replacement_unchanged_between_formats()

# Validation
def test_8_team_config_valid()
def test_7_team_config_raises_error()
def test_15_team_config_raises_error()
def test_auction_without_budget_raises_error()
def test_snake_pick_position_validated()

# Integration
def test_10_team_half_ppr_snake_produces_valid_rankings()
def test_total_auction_pool_matches_formula()
```

---

## Verification before marking complete

1. Run `python scripts/compute_valuations.py` for a half PPR 10-team league — no errors
2. Top WR in half PPR should be slightly lower value than same player in PPR
3. Derrick Henry type (high rushes, low receptions) should be higher relative value in half PPR
4. 8-team league has fewer players with positive system value (higher replacement)
5. 14-team league has more players with positive system value (lower replacement)
6. All 7 config combinations in `CONFIGS_TO_TEST` pass validation test
7. No hardcoded `12` or `ppr` strings remaining in valuation engine

---

## Commit
```
feat(saas): half PPR support and league config completion

Half PPR scoring: 0.5 points per reception throughout.
Replacement levels adjusted for half PPR format.
Valuation agent receives scoring format context.
Draft board displays scoring format badge.
LeagueConfig validation: team_count 8-14, scoring ppr/half_ppr.
All 7 standard league configurations tested end-to-end.
High-carry RBs correctly worth more in half PPR relative to WRs.
Zero hardcoded league constants remain anywhere in codebase.
Coverage: X%.
```
