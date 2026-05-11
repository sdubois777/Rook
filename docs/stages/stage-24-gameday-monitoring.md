# Stage 24: Real-Time Game Day Injury Monitoring

## Before starting, read:
- `docs/AGENTS.md` — Agent 6: Beat Reporter
- `docs/INSEASON.md`
- `docs/rules/COST_RULES.md`
- Stage 8 (Beat Reporter) and Stage 21 (Waiver Wire) must be complete

---

## Goal
During NFL games the app polls for injury news every 5 minutes and
pushes real-time alerts to the UI with waiver wire replacement
suggestions. A player getting carted off should appear as a push
notification within 5-10 minutes — fast enough to get a waiver
claim in before most of your league.

This does NOT replace the existing daily 7am Beat Reporter job.
It adds targeted polling on top of it, only during game windows.

---

## Cost target
~$21/season total for all game-day polling.
Under $0.60 per Sunday. Well within budget.

---

## Model
`claude-haiku-4-5-20251001` — 300 tokens per signal
Same as existing Beat Reporter. No Sonnet needed here.

---

## Game day schedule

```python
# All times Eastern
GAME_WINDOWS = {
    "thursday": {
        "pre_start":  "18:45",   # 90 min before TNF kickoff
        "poll_start": "20:15",   # TNF kickoff
        "poll_end":   "23:30",
    },
    "saturday": {
        # Weeks 15-18 only
        "pre_start":  "11:30",
        "poll_start": "13:00",   # first game
        "poll_end":   "23:30",
    },
    "sunday": {
        # Early (1pm), Late (4:05/4:25), SNF (8:20pm)
        "pre_start":  "11:30",   # 90 min before early games
        "poll_start": "13:00",   # first kickoff
        "poll_end":   "23:30",   # after SNF
    },
    "monday": {
        "pre_start":  "18:45",   # 90 min before MNF
        "poll_start": "20:15",   # MNF kickoff
        "poll_end":   "23:30",
    },
}
```

---

## Tasks

### 1. Create backend/utils/nfl_schedule.py

```python
from datetime import datetime, time
import pytz

ET = pytz.timezone('America/New_York')

def is_game_day() -> bool:
    """Is today a day with NFL games?"""
    today = datetime.now(ET).strftime("%A").lower()
    return today in GAME_WINDOWS

def is_in_game_window() -> bool:
    """
    Are we currently in an active game polling window?
    Returns True if we should be polling every 5 minutes.
    """

def is_pre_game_window() -> bool:
    """
    90-minute pre-game window — inactives declarations.
    Poll every 10 minutes here.
    """

def is_nfl_season() -> bool:
    """
    Are we in the regular season or playoffs?
    Returns False during offseason (Feb-Aug).
    All polling jobs check this first and exit if False.
    """

def get_current_week() -> int | None:
    """
    Returns current NFL week number (1-18).
    Returns None if offseason.
    Used to determine if Saturday games are active
    (only weeks 15-18 have Saturday games).
    """
```

**Critical:** Every polling job must call `is_nfl_season()` first
and return immediately if False. This prevents any polling during
the offseason when the app is being built and tested.

---

### 2. Add APScheduler jobs

In `backend/main.py` alongside existing daily job:

```python
# Existing daily job — unchanged
scheduler.add_job(
    beat_reporter_agent.run,
    'cron',
    hour=7,
    id='beat_reporter_daily'
)

# Pre-game: every 10 min
# Catches inactives and late scratches
scheduler.add_job(
    beat_reporter_agent.run_pregame,
    'cron',
    minute='*/10',
    id='beat_reporter_pregame',
    misfire_grace_time=60,
)

# In-game: every 5 min
# Catches in-game injuries
scheduler.add_job(
    beat_reporter_agent.run_ingame,
    'cron',
    minute='*/5',
    id='beat_reporter_ingame',
    misfire_grace_time=30,
)

# Post-game: once at 11:30pm ET on game days
# Captures snap counts and post-game injury updates
# Also triggers waiver wire agent
scheduler.add_job(
    beat_reporter_agent.run_postgame,
    'cron',
    day_of_week='thu,sat,sun,mon',
    hour=23,
    minute=30,
    timezone='America/New_York',
    id='beat_reporter_postgame',
)
```

---

### 3. Add game-day methods to Beat Reporter agent

Add to `backend/agents/beat_reporter.py`:

```python
async def run_pregame(self) -> None:
    """
    Pre-game window (90 min before kickoff).
    Runs every 10 minutes.
    Focus: inactives declarations, late scratches,
           game-time decisions resolved.
    Exits immediately if not in pre-game window
    or if not NFL season.
    """
    if not is_pre_game_window() or not is_nfl_season():
        return
    
    await self._scan_rotowire_feed(
        signal_types=["out", "doubtful", "inactive",
                      "questionable"]
    )

async def run_ingame(self) -> None:
    """
    In-game polling. Runs every 5 minutes.
    Focus: in-game injuries, ejections,
           emergency QB situations.
    Exits immediately if not in game window
    or if not NFL season.
    """
    if not is_in_game_window() or not is_nfl_season():
        return
    
    await self._scan_rotowire_feed(
        signal_types=["injury", "inactive", "out"]
    )
    await self._scan_espn_injury_feed()

async def run_postgame(self) -> None:
    """
    Post-game scan at 11:30pm ET on game days.
    Focus: snap counts, post-game injury designations,
           practice squad activations.
    Also triggers waiver wire agent.
    """
    if not is_game_day() or not is_nfl_season():
        return
    
    await self._scan_rotowire_feed(
        signal_types=["snap_count", "injury_update",
                      "transaction"]
    )
    await self._trigger_waiver_wire_analysis()
```

---

### 4. Rotowire polling implementation

Rotowire's public news page updates within minutes of injuries.
No API key required — scrape the free public feed.

```python
async def _scan_rotowire_feed(
    self,
    signal_types: list[str] = None,
) -> int:
    """
    Scrape Rotowire public NFL news feed.
    URL: https://www.rotowire.com/football/news.php
    
    Deduplicates via content hash — never writes
    the same signal twice.
    
    Returns count of new signals written to DB.
    """
    ROTOWIRE_FEED = (
        "https://www.rotowire.com/football/news.php"
    )
    
    # Parse news items from HTML
    # Each item: player_name, news_text, timestamp, type
    
    new_signals = 0
    for item in parsed_items:
        # Skip if wrong signal type
        if signal_types and item["type"] not in signal_types:
            continue
        
        # Dedup by content hash
        content_hash = hashlib.md5(
            item["text"].encode()
        ).hexdigest()
        
        existing = await self._check_signal_exists(
            content_hash
        )
        if existing:
            continue
        
        # Match to player in DB
        player_id = await self._match_player(
            item["player_name"]
        )
        
        # Write to beat_reporter_signals
        await self._write_signal(item, content_hash, player_id)
        
        # Update player injury status if applicable
        if item["type"] in ("injury", "out", "inactive"):
            await self._update_player_injury_status(
                player_id, item
            )
            # Push real-time alert to UI
            await self._push_injury_alert(item)
        
        new_signals += 1
    
    return new_signals
```

---

### 5. Real-time WebSocket injury alerts

When an injury signal is detected, push immediately to UI:

```python
async def _push_injury_alert(self, signal: dict) -> None:
    """
    Broadcast injury alert via WebSocket.
    Includes replacement suggestion from waiver wire.
    """
    from backend.websocket.manager import ws_manager
    
    # Find best available replacement
    replacement = await self._find_waiver_replacement(
        signal["player_name"],
        signal["position"],
    )
    
    await ws_manager.broadcast({
        "type": "injury_alert",
        "severity": "high",
        "player_name": signal["player_name"],
        "signal_type": signal["type"],
        "raw_text": signal["text"],
        "replacement": replacement,
        "timestamp": datetime.utcnow().isoformat(),
    })
```

---

### 6. React UI — Injury alert component

Add to the alert system that already exists from Stage 13:

```javascript
// Alert renders as:
// ┌────────────────────────────────────────────┐
// │ 🚨 INJURY ALERT                     [✕]   │
// │                                            │
// │ Bijan Robinson — Carted off, knee          │
// │ "Robinson was helped off the field..."     │
// │                                            │
// │ Suggested pickup: Tyler Allgeier (ATL)     │
// │ Available · Projected: 12.4 pts            │
// │                                            │
// │ [View Waiver Wire]  [Dismiss]              │
// └────────────────────────────────────────────┘
```

Also request browser notification permission on first load:

```javascript
useEffect(() => {
    if (Notification.permission === 'default') {
        Notification.requestPermission()
    }
}, [])

// When injury_alert received:
socket.on('injury_alert', (data) => {
    store.addAlert({...data, dismissable: true})
    
    // Browser push notification (works when app minimized)
    if (Notification.permission === 'granted') {
        new Notification(
            `Fantasy Alert: ${data.player_name}`,
            {
                body: data.replacement
                    ? `Consider adding ${data.replacement.name}`
                    : data.raw_text,
                tag: `injury-${data.player_name}`,
                // tag prevents duplicate notifications
            }
        )
    }
})
```

---

### 7. Pipeline Admin — Game day monitor panel

Add to Pipeline Admin page:

```
GAME DAY MONITOR
┌──────────────────────────────────────────────┐
│ Status: ACTIVE — Sunday in-game window       │
│ Last scan: 3 minutes ago                     │
│ Signals today: 14 new                        │
│ Injury alerts sent: 2                        │
│                                              │
│ Schedule today:                              │
│  Pre-game:  every 10 min (11:30am–1:00pm)   │
│  In-game:   every 5 min (1:00pm–11:30pm)    │
│  Post-game: once at 11:30pm                  │
│                                              │
│ [Force Scan Now]                             │
└──────────────────────────────────────────────┘
```

Off-season / off-day display:
```
GAME DAY MONITOR
Status: INACTIVE (offseason)
Polling resumes: September 2026
Daily scan: 7am
```

---

### 8. Auto-trigger waiver wire after post-game

```python
async def _trigger_waiver_wire_analysis(self) -> None:
    """
    After post-game scan, automatically run the
    waiver wire agent to surface pickup opportunities.
    
    Only runs if draft has happened (roster records exist).
    This is the primary weekly waiver wire trigger —
    runs every Tuesday at 11pm via existing schedule
    AND after every post-game scan on game days.
    """
    roster_count = await self.db.scalar(
        select(func.count(SeasonRoster.id))
    )
    if roster_count == 0:
        return  # Draft hasn't happened yet
    
    logger.info(
        "Triggering post-game waiver wire analysis"
    )
    from backend.agents.waiver_wire import WaiverWireAgent
    agent = WaiverWireAgent(self.db)
    await agent.run_weekly()
```

---

### 9. Cost logging

Log each polling job separately so Pipeline Admin
can show cost breakdown:

```python
# agent_name values for api_usage_log:
'beat_reporter_pregame'
'beat_reporter_ingame'
'beat_reporter_postgame'
'beat_reporter_daily'   # existing
```

---

## Cost estimate

```
Per scan cost: ~$0.006 (20 signals × 300 tokens × Haiku rate)

Sunday:
  Pre-game: 9 scans × $0.006  = $0.054
  In-game: 84 scans × $0.006  = $0.504
  Post-game: 1 scan × $0.006  = $0.006
  Total:                        ~$0.56

Full 17-week season:
  Sundays (17):                 $9.52
  Thursdays (15 TNF games):     $4.20
  Mondays (17 MNF games):       $4.76
  Saturdays wks 15-18 (4):     $2.24
  Total:                        ~$20.72
```

Slightly over the $20/season target but acceptable.
Many scans find zero new signals and exit in milliseconds,
so actual cost will be lower than estimate.

---

## Required test cases

```python
# tests/unit/utils/test_nfl_schedule.py
def test_is_game_day_thursday()
def test_is_game_day_tuesday_returns_false()
def test_is_in_game_window_during_window()
def test_is_in_game_window_outside_window_returns_false()
def test_is_pre_game_window_90_min_before_kickoff()
def test_is_pre_game_window_after_kickoff_returns_false()
def test_saturday_games_only_weeks_15_to_18()
def test_is_nfl_season_false_in_may()
def test_is_nfl_season_true_in_october()

# tests/unit/agents/test_beat_reporter_ingame.py
def test_run_ingame_exits_if_not_in_window()
def test_run_ingame_exits_if_offseason()
def test_run_pregame_exits_if_not_in_window()
def test_run_postgame_exits_if_not_game_day()
def test_rotowire_dedup_skips_seen_signals()
def test_content_hash_prevents_duplicate_writes()
def test_injury_signal_pushes_websocket_alert()
def test_injury_alert_includes_replacement_suggestion()
def test_browser_notification_sent_on_injury()
def test_waiver_wire_triggered_after_postgame()
def test_waiver_wire_not_triggered_before_draft()
def test_cost_logged_with_correct_agent_name()
def test_pregame_polls_every_10_min()
def test_ingame_polls_every_5_min()
```

---

## Verification before marking complete

1. **ASK USER** to confirm NFL season has started before testing
   live polling — all methods return immediately in offseason
2. Manually trigger `run_ingame()` and verify it scans Rotowire
3. Verify deduplication — run twice in 5 minutes, second run
   should find 0 new signals
4. Simulate injury alert: manually insert a test signal and
   verify WebSocket broadcast fires and appears in UI
5. Verify browser notification appears when app is minimized
6. Cost report after one Sunday shows breakdown by agent_name
7. All 19 named tests passing
8. **ASK USER** to test during a real game day

---

## Commit
```
feat(beat-reporter): add real-time game-day injury monitoring

Targeted polling: 10 min pre-game, 5 min in-game, once post-game.
Only runs Thu/Sat/Sun/Mon during NFL season — exits immediately otherwise.
Rotowire public feed scraping with content-hash deduplication.
WebSocket push delivers in-app injury alerts with replacement suggestion.
Browser push notifications when app is minimized.
Waiver wire agent auto-triggered after post-game scan.
Pipeline Admin shows game day monitor status.
Estimated cost: ~$21/season.
Coverage: X%.
```
