import browser from '../utils/browser.js'
import { postDraftEvent } from '../utils/api.js'
import { STORAGE_KEYS, DRAFT_INACTIVITY_MS } from '../utils/constants.js'
import {
  AUCTION_ROOT_SELECTOR,
  auctionRoot,
  shouldAuctionActivate,
  resolveAuctionState,
  detectAuctionEvents,
  initAuctionMemory,
} from './yahoo_auction_resolve.mjs'

/**
 * Yahoo AUCTION Draft Room poller (React client, 2026 replatform).
 *
 * Yahoo rebuilt the auction room as a React app rooted at
 * `#main-0-DraftClientBootstrap-Proxy` — the old `#draft` innerText container is
 * gone. This polls the React root every 300ms, resolves the board state with
 * resolveAuctionState() (text/structure/kebab-`ys-` anchored, `_ys_` hashes only
 * as loud-degrading fallbacks), and relays nomination/bid/clock/sold/teams +
 * a selector_health heartbeat via POST /draft/event.
 *
 * Still NOT WS interception — Yahoo's CSP blocks it; DOM polling is the reliable
 * path. The console.error hook for YOUR own bid/nomination frames runs in the
 * page's MAIN world via yahoo_draft_main.js ("world":"MAIN"). All selector logic
 * lives in yahoo_auction_resolve.mjs (fixture-tested against captured Yahoo DOM).
 */

const POLL_INTERVAL_MS = 300

// ---------------------------------------------------------------------------
// Poller
// ---------------------------------------------------------------------------

let active = false
let inactivityTimer = null

function markDraftActive() {
  browser.storage.local.set({
    [STORAGE_KEYS.ACTIVE_DRAFT]: true,
    [STORAGE_KEYS.DRAFT_PLATFORM]: 'yahoo',
  })
  if (inactivityTimer) clearTimeout(inactivityTimer)
  inactivityTimer = setTimeout(() => {
    browser.storage.local.set({ [STORAGE_KEYS.ACTIVE_DRAFT]: false })
  }, DRAFT_INACTIVITY_MS)
}

function startPoller() {
  if (active) return
  active = true
  // Flip to "live draft" off the gate (Yahoo's React client never renders the
  // old #draft node, so presence is now signalled here when the poller starts).
  window.__rook__ = true
  markDraftActive()

  let memory = initAuctionMemory()

  // Loud-degradation hook, throttled to once per field-state so a Yahoo deploy
  // that rotates the `_ys_` hashes alarms once (console.warn) and surfaces via
  // the selector_health heartbeat — instead of stalling silently.
  const warned = new Set()
  const warn = (field, level) => {
    const k = `${field}:${level}`
    if (warned.has(k)) return
    warned.add(k)
    console.warn(`Rook auction selector degraded: ${field} → ${level}`)
  }

  setInterval(async () => {
    const root = auctionRoot(document)
    if (!root) return

    const state = resolveAuctionState(root, { warn })

    // Reset the throttle for any field that recovered to a stable anchor, so a
    // FUTURE rotation re-alarms rather than staying silent under the old key.
    for (const [field, lvl] of Object.entries(state.health)) {
      if (lvl === 'primary' || lvl === 'na') {
        for (const k of Array.from(warned)) {
          if (k.startsWith(`${field}:`)) warned.delete(k)
        }
      }
    }

    const { events, next } = detectAuctionEvents(memory, state)
    memory = next

    for (const event of events) {
      if (event.type === 'nomination') markDraftActive()
      try {
        await postDraftEvent(event)
      } catch {
        // Network hiccup — drop this event, keep polling
      }
    }
  }, POLL_INTERVAL_MS)
}

// ---------------------------------------------------------------------------
// console.error listener — YOUR own bids/nominations (carry Yahoo player IDs).
// NOTE: re-verify these ['B',...]/['N',...] frames still fire on the React
// client; if not, Amendment B's ys-player[data-id] already covers nominee
// identity. Kept as-is (non-destructive) pending a live mid-nomination capture.
// ---------------------------------------------------------------------------

window.addEventListener('__yahoo_draft_action__', async (event) => {
  const data = event.detail
  const type = data[0]
  const player_id = data[3]
  const amount = data[4]

  try {
    if (type === 'N') {
      await postDraftEvent({
        type: 'my_nomination',
        platform: 'yahoo',
        payload: { yahoo_player_id: player_id, opening_bid: amount },
      })
    } else if (type === 'B') {
      await postDraftEvent({
        type: 'my_bid',
        platform: 'yahoo',
        payload: { yahoo_player_id: player_id, amount },
      })
    }
  } catch {
    // Ignore relay failures for self-actions
  }
})

// ---------------------------------------------------------------------------
// Bootstrap — wait for the React auction room, confirm the live gate, then start
// ---------------------------------------------------------------------------

// Gate: React root present AND POSITIVE auction content (a Proj-$ nominee OR
// >=1 $-budget .ys-team) AND not draft-complete. Content-only — snake shares the
// React root but its .ys-team cards are budget-less and it has no Proj-$ nominee,
// so this stays inert on a snake page without any root/marker veto.
function auctionReady() {
  return shouldAuctionActivate(document)
}

function bootstrap() {
  if (auctionReady()) {
    startPoller()
    return
  }
  const observer = new MutationObserver(() => {
    if (auctionReady()) {
      observer.disconnect()
      startPoller()
    }
  })
  observer.observe(document.documentElement, { childList: true, subtree: true })
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootstrap)
} else {
  bootstrap()
}

// Re-export so the snake poller can veto on the same constant (single source).
export { AUCTION_ROOT_SELECTOR }
