# Stage 13: Draft UI (React Frontend)

## Before starting, read:
- `docs/LIVE_DRAFT.md` — Draft UI section
- Stage 11 (Playwright bridge) and Stage 12 (Live draft agent) must be complete

---

## Goal
The entire auction draft runs from this UI. User never needs to look at
Yahoo's tab. Everything is visible, fast, and clear under pressure.

---

## Step 1 — Ask user for preferences before building anything

**ASK USER:**
1. "Dark mode or light mode default? (Can support both with a toggle)"
2. "Color scheme preference, or should I choose something clean and 
   neutral that won't be distracting during a high-stakes auction?"
3. "Layout preference for the draft board — do you want opponent 
   rosters visible during nominations, or would you rather see the 
   full player pool?"
4. "Any accessibility requirements (font size, contrast, etc.)?"

Build according to their answers. If they have no preference, use:
- Dark mode default (easier on eyes during long draft sessions)
- Blue/slate neutral palette — no distracting colors except for alerts
- Compact layout maximizing information density

---

## Layout

```
┌─────────────────────────────────────────────────────────────┐
│  ALERT BANNER (only visible when alerts are active)          │
├──────────────────────┬──────────────────────────────────────┤
│  NOMINATION PANEL    │  RECOMMENDATION CARD                  │
│  Player name         │  Action badge: BUY / BLOCK / PASS    │
│  Position / Team     │  Bid ceiling: $XX                    │
│  Clock countdown     │  Active flags                        │
│  Current bid: $XX    │  Notes (2-3 sentences)               │
│                      │  Market: $XX | System: $XX           │
├──────────────────────┴──────────────────────────────────────┤
│  BID CONTROLS                  │  NOMINATE PANEL             │
│  [-$10][-$5][-$1] $XX [+$1][+$5][+$10]  │  Search players  │
│  [   SUBMIT BID   ] [  PASS  ]  │  Opening bid: $1         │
│                                 │  [  NOMINATE  ]          │
├─────────────────────────────────┴──────────────────────────┤
│  YOUR ROSTER ($XX remaining, X slots left)                  │
│  [Player cards with price and position]                     │
├─────────────────────────────────────────────────────────────┤
│  LIVE DRAFT BOARD              │  OPPONENT BUDGETS          │
│  All picks scrollable          │  Team | Budget | Slots     │
│  Filter by position            │  (sorted by threat score)  │
└─────────────────────────────────────────────────────────────┘
```

---

## Component specifications

### Alert Banner
Position: fixed top, full width, z-index highest
Only visible when alerts are present — collapses when none active

Three alert levels:
- **MANUAL_ACTION_REQUIRED** (red, pulsing border animation)
  - Cannot be dismissed — stays until user clicks "I did it manually"
  - Shows exactly what action to take: "BID $45 ON PUKA NACUA IN YAHOO TAB"
  - Font size larger than normal — visible in peripheral vision
  
- **Combo threat alert** (orange)
  - "⚠️ [Opponent] now has CMC + Jonathan Taylor — Elite RB Stack"
  - Dismissable after reading
  
- **Block flag** (yellow)
  - "🛡️ Consider blocking — worth $XX to [Opponent] vs $XX to you"
  - Dismissable

### Nomination Panel
Appears automatically when bridge fires nomination event.
Before any nomination: shows "Waiting for nomination..." in muted text.

Fields:
- Player name (large, prominent)
- Position badge + Team badge
- Auction clock: countdown timer, color shifts green→yellow→red
  - Green: >15 seconds
  - Yellow: 8-15 seconds  
  - Red: <8 seconds (pulsing)
- Current bid amount (updates live from bridge bid_update events)
- Nominated by: [team name]

### Recommendation Card
Appears within 2 seconds of nomination (fires when live draft agent responds).
Before recommendation arrives: "Analyzing..." spinner.

Fields:
- **Action badge** (large, colored):
  - BUY → green background
  - BID_TO → blue background (bid up to ceiling but don't go over)
  - BLOCK → orange background
  - PASS → gray background
- Bid ceiling: "$XX" (bold)
- If BLOCK: shows block value and opponent it's blocking
- Active dependency flags (if any):
  - "⚠️ DISPLACED: [Allen] already drafted — target share capped"
  - "📈 BENEFICIARY: Value up if [Allen] misses time"
- Reasoning: 1-2 sentences from agent
- Market value vs system value comparison: "Market: $31 | System: $14"

### Bid Controls
- Current bid display (large number)
- Decrement buttons: -$10, -$5, -$1
- Increment buttons: +$1, +$5, +$10
- Bid amount never goes below $1 (floor enforced in UI)
- **SUBMIT BID** button: prominent, green, keyboard shortcut Enter
- **PASS** button: smaller, gray
- Keyboard shortcuts:
  - Enter → submit current bid
  - Escape → pass
  - Arrow up/down → +$1/-$1

### Nominate Panel
- Player search: type to search, results filter in real time
- Position filter buttons: ALL | QB | RB | WR | TE | K | DEF
- Results show: player name, team, tier badge, bid ceiling
- Clicking a result selects them for nomination
- Opening bid input: defaults to $1
- **NOMINATE** button: fires bridge.nominate_player()
- Shows "Waiting for your turn..." when it's not your nomination turn

### Live Draft Board
Scrollable table, most recent picks at top:
- Pick # | Player | Pos | Team | Price | Owner

Color coding by owner:
- Your picks: highlighted row
- High-threat opponent picks: subtle orange tint

Filterable by position (ALL | QB | RB | WR | TE | K | DEF)

### Opponent Budget Tracker
Sorted by threat score (highest first — most dangerous opponent at top):

| Team | Budget | Slots | Threat |
|------|--------|-------|--------|
| Opp1 | $87 | 9 | 🔴 82 |
| Opp2 | $45 | 11 | 🟡 54 |
| You | $112 | 12 | — |

- Budget bar: visual remaining budget as colored bar (green→yellow→red)
- When opponent budget < $15: gray out their row ("can't afford danger")

### Your Roster Panel
Grid of drafted players:
- Player name, position badge, price paid
- Sorted by position (QB, RB, RB, WR, WR, FLEX, K, DEF, bench...)
- Empty slots shown as dotted placeholders with position label
- Budget remaining: "💰 $XX remaining — X slots to fill"

---

## State management (Zustand)

```javascript
// store/draftStore.js
const useDraftStore = create((set) => ({
  // WebSocket connection
  connected: false,
  
  // Current nomination
  currentNomination: null,    // {player, clock, currentBid, nominatedBy}
  recommendation: null,       // Agent's recommendation
  
  // Draft state
  allPicks: [],               // All picks so far
  yourRoster: [],             // Your picks
  opponentRosters: {},        // {teamId: [picks]}
  opponentBudgets: {},        // {teamId: remaining}
  yourBudget: 200,            // Your remaining budget
  
  // Alerts
  alerts: [],                 // Active alert stack
  
  // Actions
  setNomination: (nom) => set({ currentNomination: nom, recommendation: null }),
  setRecommendation: (rec) => set({ recommendation: rec }),
  recordPick: (pick) => set((state) => ({ ... })),
  addAlert: (alert) => set((state) => ({
    alerts: [...state.alerts, alert]
  })),
  dismissAlert: (id) => set((state) => ({
    alerts: state.alerts.filter(a => a.id !== id)
  })),
}))
```

---

## WebSocket integration

```javascript
// hooks/useDraftWebSocket.js
useEffect(() => {
  const socket = io('/draft', { transports: ['websocket'] })
  
  socket.on('recommendation', (data) => {
    store.setRecommendation(data)
  })
  
  socket.on('nomination', (data) => {
    store.setNomination(data)
  })
  
  socket.on('draft_pick', (data) => {
    store.recordPick(data)
  })
  
  socket.on('MANUAL_ACTION_REQUIRED', (data) => {
    store.addAlert({
      id: Date.now(),
      type: 'MANUAL_ACTION_REQUIRED',
      ...data,
      dismissable: false
    })
  })
  
  socket.on('opponent_combo_alert', (data) => {
    store.addAlert({
      id: Date.now(),
      type: 'combo_threat',
      ...data,
      dismissable: true
    })
  })
  
  return () => socket.disconnect()
}, [])
```

---

## Required test cases

```javascript
// tests/unit/components/DraftRoom.test.jsx

test('recommendation appears within 2 seconds of nomination event')
test('BUY badge renders green')
test('BLOCK badge renders orange')  
test('PASS badge renders gray')
test('MANUAL_ACTION_REQUIRED alert cannot be dismissed')
test('MANUAL_ACTION_REQUIRED alert is visually prominent (large text, pulsing)')
test('clock countdown turns red below 8 seconds')
test('bid amount cannot go below $1')
test('Enter key submits bid')
test('Escape key passes nomination')
test('opponent budget bar grays out below $15')
test('budget summary updates after each pick')
test('your roster panel shows empty slot placeholders')
test('draft board filterable by position')
```

---

## Verification before marking complete
1. Full mock draft completable without touching Yahoo tab
2. Recommendation appears within 2 seconds of nomination
3. MANUAL_ACTION_REQUIRED alert is unmissable — large, pulsing, not dismissable
4. Budget trackers accurate throughout mock draft
5. Clock countdown colors work correctly
6. Keyboard shortcuts work (Enter to bid, Escape to pass)
7. **ASK USER** to run mock draft and give feedback on UI before committing

---

## Commit
```
feat(draft-ui): implement React draft room UI

Full auction draft control from app.
Real-time recommendations, opponent tracking, alert system.
MANUAL_ACTION_REQUIRED alert prominent and non-dismissable.
Keyboard shortcuts implemented.
Mock draft tested with user feedback incorporated.
```
