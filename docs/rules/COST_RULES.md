# API Cost Efficiency Rules — Mandatory

Every rule here is mandatory. Build them in from the start.
Retrofitting cost controls after a 30-minute pipeline run is painful.

The goal: a full pre-draft pipeline run costs under $1.50.
A full season's API usage (pipeline + weekly refreshes + draft day) costs under $20.

---

## Rule 1: Batch by team, never by player

32 teams = 32 API calls maximum for the full pipeline.
200+ players = never 200+ API calls.

```python
# WRONG — 200+ calls
for player in all_players:
    await client.messages.create(...)

# CORRECT — 32 calls
for team in all_teams:
    team_players = [p for p in all_players if p.team == team]
    await client.messages.create(
        content=build_team_batch_prompt(team, team_players)
    )
```

---

## Rule 2: Hash-based caching

Before every API call, hash the input data.
If the hash matches a stored result, skip the API call entirely.

```python
input_hash = hashlib.sha256(
    json.dumps(input_data, sort_keys=True, default=str).encode()
).hexdigest()

cached = await db.get_cached_result(agent_name, entity_id, input_hash)
if cached:
    return json.loads(cached.output_json)  # Free — no API call
```

Add `input_hash VARCHAR(64)` to every agent output table.

**What triggers a re-run:**
- Team Systems: coaching or roster changes since last run
- Roster Changes: new transactions since last run
- Player Profiles: team system grade changed, or new target share data
- Injury Risk: new injury log entry
- Schedule: NFL schedule updated, or opponent defensive roster changed
- Beat Reporter: always re-runs (freshness layer, runs daily)

During the season, a weekly refresh should touch ~5-15 players, not all 200.

---

## Rule 3: Model tiering

| Task type | Model | Max tokens |
|-----------|-------|-----------|
| Data extraction, formatting | `claude-haiku-4-5-20251001` | 500 |
| Team batch (extraction) | `claude-haiku-4-5-20251001` | 1000 |
| Roster changes (reasoning) | `claude-sonnet-4-6` | 2000 |
| Trade analysis | `claude-sonnet-4-6` | 1500 |
| Live draft recommendation | `claude-sonnet-4-6` | 400 |

Default is Haiku. Upgrade to Sonnet only for multi-step causal reasoning.

---

## Rule 4: JSON-only output

Every agent prompt: `Output ONLY valid JSON. No preamble. No markdown.`
This eliminates wasted output tokens on prose the code discards anyway.

---

## Rule 5: Pre-aggregate in Python

Never pass raw data into a prompt. Aggregate in Python first (free),
pass only summaries (cheap).

```python
# WRONG — passes thousands of rows to the model
prompt = f"Here is the play-by-play: {raw_pbp.to_json()}"

# CORRECT — aggregate first
summary = {
    "target_share_by_player": pbp.groupby("receiver")["target"].mean().to_dict(),
    "air_yards_by_player": pbp.groupby("receiver")["air_yards"].sum().to_dict(),
}
prompt = f"Given these stats: {json.dumps(summary)}"
```

---

## Rule 6: Explicit max_tokens on every call

Every `messages.create()` call must have `max_tokens` set explicitly.
Never omit it. Reference values:

```python
MAX_TOKENS = {
    "team_system_grade":          500,
    "player_profile_batch":      1000,
    "roster_changes_team":       2000,
    "injury_risk_batch":         1000,
    "schedule_batch":            1500,  # 3-position JSON (WR/RB/TE) needs ~1100-1200 tokens
    "beat_reporter_signal":       300,
    "live_draft_recommendation":  400,
    "trade_analysis":            1500,
    "lineup_recommendation":     1000,
    "waiver_wire_weekly":         800,
}
```

---

## Rule 7: Dry run mode

Every pipeline script supports `--dry-run`:
- Logs every API call that would be made
- Shows cache hits (skipped calls)
- Prints total estimated cost
- Does NOT call the API

Always run `--dry-run` before the first real run of any new agent.

---

## Rule 8: Cost estimate + confirmation

Before any pipeline run making more than 10 API calls:

```python
print(f"Estimated: {n_calls} API calls, ${estimated_cost:.4f}")
confirm = input("Proceed? (yes/no): ")
if confirm != "yes":
    sys.exit(0)
```

---

## Rule 9: Partial runs

Support `--agent` and `--team` flags on all pipeline scripts:

```bash
# Refresh one team after a trade
python scripts/run_predraft_pipeline.py --agent roster_changes --team LAC

# Daily freshness only
python scripts/run_predraft_pipeline.py --agent beat_reporter

# Full run (only before draft or start of season)
python scripts/run_predraft_pipeline.py --agent all
```

---

## Rule 10: Token usage logging

Every API call logs to `api_usage_log` table:

```sql
CREATE TABLE api_usage_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_name VARCHAR(50),
    model VARCHAR(50),
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost_usd DECIMAL(8,6),
    cache_hit BOOLEAN DEFAULT false,
    entity_id VARCHAR(100),
    called_at TIMESTAMP DEFAULT NOW()
);
```

If a call is made without logging usage, it is a bug.

---

## Pricing constants (update if Anthropic changes pricing)

```python
HAIKU_INPUT_PER_MTK   = 0.80   # per million tokens
HAIKU_OUTPUT_PER_MTK  = 4.00
SONNET_INPUT_PER_MTK  = 3.00
SONNET_OUTPUT_PER_MTK = 15.00
```

---

## Expected cost benchmarks

If your agent run significantly exceeds these, something is wrong:

| Agent | Calls | Model | Expected cost |
|-------|-------|-------|--------------|
| Team Systems (32 teams) | 32 | Haiku | ~$0.05 |
| Roster Changes (32 teams) | 32 | Sonnet | ~$0.40 |
| Player Profiles (32 batches) | 32 | Haiku | ~$0.10 |
| Injury Risk (32 batches) | 32 | Haiku | ~$0.08 |
| Schedule (32 batches) | 32 | Haiku | ~$0.06 |
| Beat Reporter (daily) | 10-20 | Haiku | ~$0.02/day |
| **Full pipeline** | ~200 | Mixed | **~$1.00** |
| Weekly in-season refresh | 20-40 | Mixed | ~$0.20/week |
