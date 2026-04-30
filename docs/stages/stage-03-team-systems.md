# Stage 3: Team Systems Agent

## Before starting, read:
- `docs/AGENTS.md` — Agent 1: Team Systems spec
- `docs/rules/COST_RULES.md`
- `docs/rules/PATTERNS.md`

---

## Goal
All 32 NFL teams have system grade records in the `team_systems` table.
This agent runs first — all other agents inherit its output.

---

## Model and cost parameters
- Model: `claude-haiku-4-5-20251001` — data extraction, NOT reasoning
- Max tokens: 500 per team
- Total API calls: 32 (one per team, never per player)
- Pattern: pre-aggregate in Python → single Haiku call → parse JSON → write DB

---

## Tasks

### 1. BaseAgent implementation
Create `backend/agents/base_agent.py`:

```python
class BaseAgent:
    def __init__(self, model: str, max_tokens: int, agent_name: str, dry_run: bool = False):
        # model and max_tokens are REQUIRED — no defaults
        # Every subclass must declare its own values
        self._client = anthropic.AsyncAnthropic()
        self.model = model
        self.max_tokens = max_tokens
        self.agent_name = agent_name
        self.dry_run = dry_run

    async def call_once(self, system: str, user: str, input_data: dict) -> str:
        # 1. Hash input_data
        # 2. Check agent_cache table
        # 3. If cache hit: log as cache_hit=True, return cached output
        # 4. If dry_run: print estimate, return ""
        # 5. Call client.messages.create() with self.model and self.max_tokens
        # 6. Log to api_usage_log
        # 7. Write to agent_cache
        # 8. Return response text
        pass
```

`run_agent()` (iterative tool-use loop) stays in a separate file:
`backend/agents/agent_loop.py` — only imported by `live_draft.py`.

Pre-draft pipeline agents use ONLY `BaseAgent.call_once()`.

### 2. Team Systems agent
Create `backend/agents/team_systems.py`:

```python
from backend.utils.seasons import get_current_season, get_analysis_seasons

class TeamSystemsAgent(BaseAgent):
    AGENT_MODEL = "claude-haiku-4-5-20251001"
    AGENT_MAX_TOKENS = 500

    async def _build_team_context(self, team_abbr: str) -> dict:
        # Pre-aggregate ALL data in Python — no API calls here
        # Pull from nfl_data.py and overthecap wrappers
        # Return compact summary dict

    async def run_for_team(self, team_abbr: str) -> dict | None:
        context = await self._build_team_context(team_abbr)
        raw = await self.call_once(
            system=SYSTEM_PROMPT,
            user=json.dumps(context),
            input_data=context,
        )
        data = parse_json_output(raw)
        await self._write_to_db(data)
        return data

    async def run_all_teams(self, concurrency: int = 4) -> dict[str, bool]:
        # Pre-warm data caches ONCE before concurrent runs
        # Use asyncio.Semaphore(concurrency) + asyncio.gather()
```

### 3. Key logic to implement
- `rookie_qb_flag`: true for any first-year NFL starter
- `compound_risk_flag`: true when rookie_qb_flag AND pass_protection_grade in ["C", "C-", "D+", "D", "F"]
- O-line split: pass protection grade and run blocking grade stored SEPARATELY
- OC history: pull from last 3 coaching stops, not just current season
- All season references via `get_current_season()` and `get_analysis_seasons()`

### 4. Pipeline script
Update `scripts/run_predraft_pipeline.py` to support:
```bash
python scripts/run_predraft_pipeline.py --agent team_systems --dry-run
python scripts/run_predraft_pipeline.py --agent team_systems --team LAC
python scripts/run_predraft_pipeline.py --agent team_systems
```

---

## Required test cases
```python
# tests/unit/agents/test_team_systems.py
def test_single_api_call_per_team()  # run_for_team makes exactly ONE call_once()
def test_rookie_qb_flag_first_year_starter()
def test_rookie_qb_flag_false_veteran()
def test_compound_risk_flag_rookie_qb_bad_line()
def test_compound_risk_flag_false_veteran_qb()
def test_compound_risk_flag_false_rookie_qb_good_line()
def test_oline_grades_stored_separately()  # not one combined grade
def test_no_hardcoded_years()  # scan for literal year integers in file
def test_all_32_teams_in_nfl_teams_list()
def test_dry_run_makes_no_api_calls()
def test_cache_hit_skips_api_call()
def test_output_written_to_team_systems_table()
```

---

## Verification before marking complete
1. Run `--dry-run` — estimate looks reasonable (~$0.05)
2. **ASK USER** to approve cost estimate before real run
3. Run for all 32 teams — all succeed
4. Spot-check 5-6 teams: rookie QB teams flagged, system grades look reasonable
5. Compound risk flag fires correctly for rookie QB + bad O-line teams
6. No hardcoded years anywhere in `team_systems.py`
7. All unit tests passing, coverage 80%+

---

## Commit
```
feat(team-systems): implement Team Systems Agent

All 32 teams graded. Haiku model, 500 token ceiling.
Rookie QB and compound risk flags working correctly.
Single API call per team via BaseAgent.call_once().
Coverage: X%. All named tests passing.
```

---

## Ask user
- To review dry-run cost estimate before running
- To spot-check system grades for 5 teams they know well
