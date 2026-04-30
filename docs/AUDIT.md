# Project Audit Instructions

Read CLAUDE.md, docs/rules/PATTERNS.md, and docs/rules/COST_RULES.md fully
before starting this audit. Then audit the entire codebase against the rules.

---

## Audit checklist — check every item and report findings

### 1. Hardcoded season years
Search every Python file for hardcoded year integers (2022, 2023, 2024, 2025).
Any found outside of `backend/utils/seasons.py` itself is a bug.
Fix: replace with calls to `get_current_season()`, `get_analysis_seasons()`, or `get_analysis_year()`.

```bash
grep -rn "202[0-9]" backend/ --include="*.py" | grep -v seasons.py | grep -v ".pyc"
```

### 2. Model strings
Search all Python files for model strings.
Only two valid values exist in this project:
- `claude-haiku-4-5-20251001`
- `claude-sonnet-4-6`

Any other model string (claude-sonnet-4-5, claude-haiku-4-5, etc.) is a bug.

```bash
grep -rn "claude-" backend/ --include="*.py"
```

Expected results:
- team_systems.py → haiku
- roster_changes.py → sonnet
- player_profiles.py → haiku
- injury_risk.py → haiku
- schedule_agent.py → haiku
- beat_reporter.py → haiku
- live_draft.py → sonnet
- trade_analyzer.py → sonnet
- trade_proposal.py → sonnet
- opponent_analyzer.py → sonnet

### 3. Iterative tool-use loops in pipeline agents
Search for `run_agent(` outside of `live_draft.py`.
Any found in a pre-draft pipeline agent is a bug.

```bash
grep -rn "run_agent(" backend/ --include="*.py"
```

Expected: only appears in `backend/engines/live_draft.py`.

### 4. Direct API calls bypassing BaseAgent
Search for `messages.create(` outside of `base_agent.py`.
Any found in an agent file that should use BaseAgent is a bug.

```bash
grep -rn "messages.create(" backend/ --include="*.py"
```

Expected: only in `backend/agents/base_agent.py`.

### 5. Polling in live draft event chain
Search for sleep calls inside event-handling code.

```bash
grep -rn "asyncio.sleep\|time.sleep" backend/integrations/yahoo_playwright.py
grep -rn "asyncio.sleep\|time.sleep" backend/websocket/
```

Expected: `asyncio.sleep` only in health_check_loop (with a clear comment),
never inside WebSocket frame handlers or nomination handlers.

### 6. max_tokens missing or wrong
Every `messages.create()` call must have `max_tokens` set.
Check that BaseAgent enforces this and no agent bypasses it.

### 7. Missing api_usage_log entries
Every API call must log to `api_usage_log`. Verify `BaseAgent.call_once()`
always writes a log entry — on both cache hits and real calls.

### 8. Missing agent_cache logic
Every agent must check the cache before calling the API.
Verify `BaseAgent.call_once()` checks `agent_cache` table before making a real call.

### 9. Bulk vs N+1 DB operations
Search for DB queries inside loops.

```bash
grep -rn "await session.execute" backend/agents/ --include="*.py" -A 3
```

Flag any pattern where `session.execute` appears inside a `for` loop over players or flags.
These should be bulk operations.

### 10. JSON-only output enforcement
Every agent system prompt must contain the instruction:
"Output ONLY valid JSON. No explanation. No preamble. No markdown."

```bash
grep -rn "SYSTEM_PROMPT" backend/agents/ --include="*.py" -A 20 | grep -i "json\|preamble\|markdown"
```

Flag any agent whose system prompt does not include this instruction.

### 11. Data pre-aggregation
In each agent's `_build_team_context()` or equivalent:
- Should return a compact dict of pre-aggregated statistics
- Should NOT return raw DataFrames or large raw data structures
- Should NOT call the Anthropic API

Flag any agent where the context-building function makes API calls.

### 12. seasons.py usage in data layer
Check `backend/integrations/nfl_data.py`:
- Default season parameters should use `get_current_season()` not hardcoded values
- Any function with `season: int = 2024` is a bug

---

## Report format

For each issue found, report:
1. File and line number
2. What the issue is
3. What the fix should be

Then fix all issues found and run:
```bash
pytest tests/unit/ -v
```

All tests must pass after fixes. If any new test failures appear from the fixes,
resolve them before committing.

Commit all audit fixes as:
```
fix(audit): resolve issues found in project audit

[list each fix briefly]
All unit tests passing post-fix.
```

---

## After the audit
Update `CLAUDE.md` project status checklist to reflect current actual state of completed stages.
If a stage is partially complete, mark it as such with a note.
