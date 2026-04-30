# Code Patterns — Read Before Writing Any Agent

These patterns represent deliberate architectural decisions.
They often differ from common examples found online.
Follow them exactly — do not substitute the "common" pattern.

---

## Pattern 1: Dynamic Season Years

**WRONG — never do this:**
```python
CURRENT_SEASON = 2024
ANALYSIS_YEAR = 2025
for season in [2022, 2023, 2024]:
    ...
```

**CORRECT — always do this:**
```python
from backend.utils.seasons import get_current_season, get_analysis_seasons, get_analysis_year

CURRENT_SEASON = get_current_season()
ANALYSIS_YEAR = get_analysis_year()
ANALYSIS_SEASONS = get_analysis_seasons(lookback=3)

for season in ANALYSIS_SEASONS:
    ...
```

`seasons.py` derives everything from the current date. No code changes needed year-over-year.
If you see any hardcoded year in any agent file, it is a bug. Fix it immediately.

---

## Pattern 2: Pre-aggregate in Python, then one API call per team

This is the most important pattern. The default agent pattern (iterative tool-use loop)
is WRONG for pre-draft pipeline agents. It causes 5-20 API round-trips per team.

**WRONG — iterative tool-use loop:**
```python
# This makes 5-20 API calls per team. Never do this for pipeline agents.
raw = await run_agent(
    system_prompt=SYSTEM_PROMPT,
    user_message=user_message,
    tools=TOOLS,
    tool_handler=handle_tool,
    max_tokens=4096,
)
```

**CORRECT — pre-aggregate, single call:**
```python
# Step 1: Fetch all data in Python (free, no API calls)
context = await _build_team_context(team_abbr)

# Step 2: ONE API call with full context already loaded
response = await base_agent.call_once(
    model=AGENT_MODEL,
    max_tokens=AGENT_MAX_TOKENS,
    system=SYSTEM_PROMPT,
    user=f"Analyze this pre-aggregated data: {json.dumps(context)}",
)

# Step 3: Parse JSON output
flags = parse_json_output(response)
```

**The test:** A pre-draft pipeline agent's `run_for_team()` function must contain
exactly ONE call to the API. If there is more than one, the implementation is wrong.

---

## Pattern 3: Data caching — load once, reuse across all 32 teams

**WRONG — reloads the full dataset for every player lookup:**
```python
async def get_player_history(player_name: str):
    for season in ANALYSIS_SEASONS:
        ts = nfl_data.compute_target_share(season)  # Reloads every time!
        ...
```

**CORRECT — pre-warm cache once, slice per player:**
```python
# In run_all_teams(), before any concurrent team runs:
for season in ANALYSIS_SEASONS:
    ts = nfl_data.compute_target_share(season)
    _set_cached_data(f"target_share_{season}", ts)

# In _fetch_target_shares_for_roster():
for season in ANALYSIS_SEASONS:
    ts_df = _get_cached_data(f"target_share_{season}")  # Already loaded
    # Slice for this team's players
    match = ts_df[ts_df["player_name"].str.contains(last, ...)]
```

---

## Pattern 4: Bulk DB operations — never N+1 queries

**WRONG — one DB query per player per flag:**
```python
for flag in flags:
    player_id = await _resolve_player_id(session, flag["player_name"])  # N queries
    trigger_id = await _resolve_player_id(session, flag["trigger_name"])  # N more queries
```

**CORRECT — collect all names, one query, resolve from memory:**
```python
# Collect all names that need resolving
names_and_teams = [(f["player_name"], f["player_team"]) for f in flags]
names_and_teams += [(f["trigger_name"], f["trigger_team"]) for f in flags]

# One bulk query
id_map = await _bulk_resolve_player_ids(session, names_and_teams)

# Resolve from in-memory map
for flag in flags:
    player_id = id_map.get((flag["player_name"], flag["player_team"]))
```

---

## Pattern 5: JSON-only output — no prose

Every agent system prompt must include this instruction. Without it, the model
adds explanation text that wastes output tokens.

```python
SYSTEM_PROMPT = """...your agent instructions...

Output ONLY a valid JSON array. No explanation, no preamble, no markdown fences.
Your entire response must be parseable by json.loads().
"""
```

Always strip accidental markdown fences in the parser:
```python
def parse_json_output(raw: str) -> list | dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())
```

---

## Pattern 6: run_agent() is only for the live draft agent

`run_agent()` in `base_agent.py` runs an iterative tool-use loop.
It is appropriate ONLY for the live draft agent, which needs to reason
interactively under time pressure.

Pre-draft pipeline agents use `BaseAgent.call_once()` instead.
In-season agents use `BaseAgent.call_once()` instead.

If you find yourself importing `run_agent` in any file other than
`backend/engines/live_draft.py`, stop and reconsider.

---

## Pattern 7: No polling in the live draft event chain

**WRONG:**
```python
while True:
    state = check_draft_state()  # polling
    await asyncio.sleep(0.5)
```

**CORRECT:**
```python
# WebSocket interception — fires on event, not on timer
async def handle_ws(ws):
    async def handle_frame(frame):
        data = parse_yahoo_frame(frame.payload)
        await handle_draft_event(data)
    ws.on("framereceived", handle_frame)
page.on("websocket", handle_ws)
```

The Playwright bridge must use WebSocket interception as primary,
MutationObserver as secondary fallback. No `asyncio.sleep()` polling loops.

---

## Pattern 8: BaseAgent usage

Never call `anthropic.AsyncAnthropic().messages.create()` directly in agent files.
Always go through `BaseAgent` which enforces caching, logging, and dry-run mode.

```python
# Every agent file
class TeamSystemsAgent(BaseAgent):
    AGENT_MODEL = "claude-haiku-4-5-20251001"
    AGENT_MAX_TOKENS = 500

    async def run_for_team(self, team_abbr: str) -> dict:
        context = await self._build_context(team_abbr)
        return await self.call_once(
            system=SYSTEM_PROMPT,
            user=json.dumps(context),
        )
```

`BaseAgent.call_once()` handles: cache check, API call, usage logging, cache write.

---

## Pattern 9: Model strings — exact values

Use only these exact strings. No other model strings exist in this project.

```python
HAIKU  = "claude-haiku-4-5-20251001"   # data extraction
SONNET = "claude-sonnet-4-6"            # causal reasoning
```

These are the only valid values for the `model` parameter anywhere in the codebase.
`claude-sonnet-4-5` does not exist. `claude-haiku-4-5` does not exist.

---

## Pattern 10: Dry run before first run

Before running any agent pipeline for the first time, always run with `--dry-run`:

```bash
python scripts/run_predraft_pipeline.py --agent team_systems --dry-run
```

Dry run prints estimated API calls, token counts, and cost. Confirm with user before
proceeding to a real run.
