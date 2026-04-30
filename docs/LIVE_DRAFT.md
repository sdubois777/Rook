# Live Draft: Yahoo Integration and Playwright Bridge

---

## Yahoo Integration Overview

Two separate integrations — use each for its appropriate scope:

**Yahoo Fantasy API (official):** League data, rosters, player universe, post-draft sync.
Do NOT attempt to use for live draft control — it doesn't support it.

**Playwright bridge:** Live draft room only. Nominations, bids, real-time event detection.

---

## Yahoo Official API Setup

ASK USER before starting this work:
1. Do you have a Yahoo Developer account? If not: developer.yahoo.com → create account → register app with Fantasy Sports scope
2. Provide `YAHOO_CLIENT_ID` and `YAHOO_CLIENT_SECRET`
3. Provide `YAHOO_LEAGUE_ID` (found in league URL)

OAuth 2.0 flow:
- Route `GET /auth/yahoo` → redirect to Yahoo authorization URL
- Route `GET /auth/yahoo/callback` → exchange code for tokens
- Store `YAHOO_REFRESH_TOKEN` in `.env` after first auth
- Auto-refresh tokens on expiry — never require user re-auth mid-season

File: `backend/integrations/yahoo_api.py`
Functions: `get_league()`, `get_teams()`, `get_players()`, `get_draft_results()`, `get_rosters()`

---

## Playwright Draft Room Bridge

Yahoo's draft room is a JavaScript web app communicating via WebSocket.
The bridge intercepts those WebSockets and exposes clean Python events.

File: `backend/integrations/yahoo_playwright.py`

### Zero polling requirement

Every layer uses events or WebSockets. No `asyncio.sleep()` polling loops anywhere.

```
Yahoo WS frames → Playwright interception → FastAPI WS → React UI
                                                              ↓
Yahoo ← Playwright page.evaluate() ← FastAPI endpoint ← User action
```

Target: under 100ms round-trip latency for draft event detection.

### Primary: WebSocket interception

```python
async def intercept_draft_websocket(page):
    async def handle_ws(ws):
        async def handle_frame(frame):
            data = parse_yahoo_frame(frame.payload)
            if data.get("type") == "nomination":
                await handle_nomination(data)
            elif data.get("type") == "bid":
                await handle_bid_update(data)
            elif data.get("type") == "draft_pick":
                await handle_pick_confirmed(data)
            elif data.get("type") == "clock_warning":
                await handle_clock_warning(data)
        ws.on("framereceived", handle_frame)
    page.on("websocket", handle_ws)
```

### Secondary fallback: MutationObserver

Inject into the Yahoo page DOM. Fires on DOM mutations.
Used as fallback if WebSocket interception misses events.

### Health check and auto-reconnect

```python
async def health_check_loop():
    while True:
        await asyncio.sleep(10)
        if not await ping_draft_room():
            await reconnect()
```

### Failure handling — mandatory

If bridge fails mid-draft, IMMEDIATELY emit `MANUAL_ACTION_REQUIRED`:

```python
async def on_bridge_failure(self, action: str, amount: int = None):
    await ws_manager.emit({
        "type": "MANUAL_ACTION_REQUIRED",
        "action": action,
        "amount": amount,
        "urgency": "high",
        "message": f"Bridge failed — manually {action} in Yahoo tab"
    })
```

Never crash silently. User must always know what action to take manually.

### Draft room URL

ASK USER for the Yahoo draft room URL format before implementing.
Ask them to navigate to last year's draft recap or this year's draft lobby and share the URL pattern.

### Yahoo WebSocket frame format

ASK USER to capture real WS frames:
Open Yahoo draft room → Browser DevTools → Network tab → WS connections.
Record payloads for: nomination, bid update, draft pick confirmed, clock warning.
Commit these as `tests/fixtures/yahoo_ws_frames.json`.

---

## Live Draft Agent

File: `backend/engines/live_draft.py`

Model: `claude-sonnet-4-6` (real-time decision-making)
Max tokens: 400 per recommendation

When a player is nominated:
1. Pull player record from draft bible (single DB query by yahoo_player_id)
2. Check dependency flags against already-drafted players
   - If trigger player already drafted → activate flag, adjust bid ceiling
3. Calculate adjusted bid ceiling given live state
4. Check opponent threat scores → determine block value
5. Compare against remaining budget
6. Output recommendation within 2 seconds

### Recommendation output format

```json
{
  "player_name": "Ladd McConkey",
  "action": "pass",
  "bid_ceiling": 14,
  "block_value": 22,
  "budget_allows_block": false,
  "active_flags": ["DISPLACED: Allen already drafted — target share capped"],
  "opponent_alerts": [],
  "notes": "Ceiling dropped from $28 to $14 with Allen off board.",
  "system_value": 28,
  "market_value": 31,
  "adjusted_system_value": 14,
  "budget_summary": {
    "your_remaining": 87,
    "roster_slots_remaining": 6,
    "minimum_completion_budget": 36,
    "spendable_on_this_player": 51
  }
}
```

### Opponent modeling

Per-opponent profile updated after every pick:
```json
{
  "team_name": "Opponent",
  "roster": [...],
  "budget_remaining": 45,
  "positional_scores": {"QB": 0.2, "RB": 0.9, "WR": 0.4, "TE": 0.3},
  "threat_score": 78,
  "combo_flags": ["Elite RB stack — historically dominant"],
  "apparent_strategy": "hero_rb"
}
```

Block flag suppressed when opponent budget < $15.

---

## Draft UI (React)

File: `frontend/src/pages/DraftRoom.jsx`

Components needed:
- Current nomination panel (player, clock countdown)
- Recommendation card (action badge, bid ceiling, flags, notes)
- Bid controls (increment/decrement, submit, pass)
- Nominate panel (search, select, opening bid, submit)
- Live draft board (all picks, prices, teams)
- Opponent budget tracker
- Your roster panel
- Alert banner (combo threats, block alerts, manual action required)

ASK USER for UI preferences before building (color scheme, dark/light mode).

---

## Testing the bridge

ASK USER to set up a practice/mock draft on Yahoo for bridge testing.

Required tests before real draft:
- Nomination detected in under 100ms (mocked WebSocket)
- Bid placement fires correctly
- Bridge failure alert appears in UI
- No polling anywhere in event chain (`test_no_polling_in_event_chain`)

Run at least 2 full mock drafts through the app before the real draft.
