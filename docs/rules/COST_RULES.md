# API Cost Efficiency Rules — Mandatory

Every rule here is mandatory. Build them in from the start.
Retrofitting cost controls after a 30-minute pipeline run is painful.

The goal WAS: a full pre-draft pipeline run under $1.50, a full season under $20.
**MEASURED REALITY (July 2026, from `api_usage_log`): a full sweep costs $9-$72.**
The three most recent multi-agent sweeps came in at $9.03, $15.95 and $17.04; the
heaviest observed day was $71.77. Lifetime spend across all runs is ~$404.
Treat the numbers below as TARGETS THAT ARE NOT BEING MET, not as descriptions.
Query `api_usage_log` (agent_name, model, input_tokens, output_tokens,
estimated_cost_usd, cache_hit, called_at) before quoting any figure from this file.

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

DESIGN TARGET (left) vs MEASURED per-non-cached-call cost from `api_usage_log` (right):

| Agent | Target calls | Model | Target cost | Measured $/call |
|-------|-------|-------|--------------|--------------|
| Team Systems (32 teams) | 32 | Haiku | ~$0.05 | $0.0032 |
| Roster Changes (32 teams) | 32 | Sonnet | ~$0.40 | $0.0690 |
| Player Profiles (32 batch + ~90 Sonnet) | ~120 | Mixed | ~$1.20 | $0.0118 |
| Injury Risk (32 batches) | 32 | Haiku | ~$0.08 | $0.0125 |
| Schedule (32 batches) | 32 | Haiku | ~$0.06 | $0.0070 |
| Beat Reporter (daily) | 10-20 | Haiku | ~$0.02/day | $0.0005 |
| Valuation Agent | 60 | Mixed | — | $0.0126 |
| **Full pipeline** | ~280 | Mixed | **~$2.00** | **$9-$72 observed** |

**The "~90 Sonnet" figure for Player Profiles is the big miss.** Measured on a real
board: **584 of 653 valued players** took a per-player Sonnet call — roughly 6.5x the
assumption, and the reason a full sweep lands an order of magnitude over target. The
routing trigger (`needs_sonnet_reasoning`) sends the entire low-usage tail to Sonnet,
which is exactly where the model has least to say. This is the single biggest cost lever
in the system and it is also implicated in projection quality (see the bucketed-output
problem in the projection layer).
