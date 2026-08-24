import browser from '../utils/browser.js'
import { postDraftEvent } from '../utils/api.js'
import {
  STORAGE_KEYS,
  DRAFT_INACTIVITY_MS,
  MAX_CAPTURED_FRAMES,
} from '../utils/constants.js'
import {
  extensionAlive,
  noteHealthyRelay,
  recoverInvalidatedContext,
  resetContextReloadCap,
} from '../utils/context_recovery.js'
import {
  parseFrame,
  isDraftFrame,
  parseUserId,
  draftIdFromTopic,
  draftIdFromPath,
} from './sleeper_resolve.mjs'
import { initSnakeMemory, detectSnakeEvents } from './sleeper_snake_resolve.mjs'
import { initAuctionMemory, detectAuctionEvents } from './sleeper_auction_resolve.mjs'
import { createGapReconciler } from './sleeper_gap_sync.mjs'

/**
 * Sleeper Draft Room poller — ISOLATED world (Phoenix Channels over WS).
 *
 * The MAIN-world interceptor (sleeper_draft_main.js) re-broadcasts each WS frame
 * via window.postMessage; this listens, parses the Phoenix frame, and routes it
 * to the snake or auction resolver (chosen in-band from the `type` field). No DOM
 * selectors — the entire draft is clean JSON on the `draft:<id>` channel, so this
 * is the most robust poller of the three platforms.
 *
 * Self-team: my Sleeper `user_id` from the page's localStorage → `draft_order`
 * maps it to my slot (drives your_turn / is_yours). Player resolution is by
 * Sleeper id (exact, backend-side). Contract emitted verbatim → downstream
 * untouched. Pure parse/detect logic lives in the resolver modules (unit-tested).
 */

let format = null // 'snake' | 'auction'
let snakeMem = null
let auctionMem = null
let inactivityTimer = null
let myUserId = null
let knownDraftId = null // from WS frame topics (URL is the other source)

function getMyUserId() {
  if (myUserId == null) {
    try {
      // Sleeper stores user_id JSON-encoded ("\"123\"") → unwrap to the bare id
      // so it matches the draft_order keys (else is_yours never resolves).
      myUserId = parseUserId(window.localStorage.getItem('user_id'))
    } catch {
      myUserId = null
    }
  }
  return myUserId
}

function markDraftActive() {
  browser.storage.local.set({
    [STORAGE_KEYS.ACTIVE_DRAFT]: true,
    [STORAGE_KEYS.DRAFT_PLATFORM]: 'sleeper',
  })
  if (inactivityTimer) clearTimeout(inactivityTimer)
  inactivityTimer = setTimeout(() => {
    browser.storage.local.set({ [STORAGE_KEYS.ACTIVE_DRAFT]: false })
  }, DRAFT_INACTIVITY_MS)
}

/** Format from the in-band `type`, falling back to auction-only event names. */
function detectFormat(frame) {
  const t = frame.payload && frame.payload.type
  if (t === 'snake' || t === 'auction' || t === 'linear') return t === 'linear' ? 'snake' : t
  if (['new_draft_offer', 'draft_updated_by_offer', 'draft_updated_by_nomination'].includes(frame.event)) {
    return 'auction'
  }
  return null
}

/** Convert the absolute Phoenix deadline → the contract's seconds/MM:SS at post time. */
function withClock(payload) {
  if (payload && payload.clock_ends_at) {
    const secs = Math.max(0, Math.round((Date.parse(payload.clock_ends_at) - Date.now()) / 1000))
    payload.seconds_remaining = secs
    payload.clock = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`
  }
  return payload
}

// Events that prove the draft is live. bid_update included: a Sleeper auction can
// sit in a long bidding war with no nomination/pick for stretches longer than the
// inactivity window — without it, the popup flipped to "inactive" mid-auction.
const ACTIVITY_EVENTS = new Set([
  'your_turn',
  'snake_pick',
  'nomination',
  'draft_pick',
  'bid_update',
])

// ---------------------------------------------------------------------------
// FULL-STATE SYNC — the recovery net. Sleeper publishes the ENTIRE draft
// (every pick, with players + slots) on its public REST API, so the backend can
// reconcile no matter what this content script missed: a dead/unpatched WS, an
// extension reload mid-draft, a page refresh — anything. We just tell it WHICH
// draft (from the page URL or the frame topic) and WHO we are; it fetches,
// records the missing picks idempotently, and pushes them to the Rook UI.
// Runs at startup + every 30s + immediately on entering a draft channel — so
// even with ZERO frames flowing, capture degrades to 30s latency, never to
// permanent desync.
// ---------------------------------------------------------------------------
const DRAFT_SYNC_INTERVAL_MS = 30 * 1000

async function syncDraftState() {
  const draftId = draftIdFromPath(location.pathname) || knownDraftId
  if (!draftId || !extensionAlive()) return
  // NULL-USER GUARD (shared by the idle nets too): a reconciliation with an
  // unresolved user_id attributes every pick is_yours=False and files your own
  // autopicked picks onto opponent rosters — and backend is_drafted dedup makes
  // that stick against later correct syncs. Skip + warn instead; the fast lane
  // (gap reconciler) retries on a short backoff and the 30s poll backstops.
  if (getMyUserId() == null) {
    console.warn(
      'Rook Sleeper: draft_sync skipped — user_id not resolved yet ' +
        '(avoids mis-filing own picks to opponents); will retry.'
    )
    return
  }
  try {
    await postDraftEvent({
      type: 'draft_sync',
      platform: 'sleeper',
      payload: { draft_id: draftId, my_user_id: getMyUserId() },
    })
  } catch {
    // Network hiccup — the next interval retries.
  }
}

// Fast lane: a `draft_updated_by_pick` frame means "a pick happened" even when
// Sleeper sent no `player_picked` for it (opening autopicks). Debounced +
// single-in-flight so an autopick burst collapses into minimal REST syncs, and
// deferred while user_id is unresolved so recovered own-picks aren't mis-filed.
const gapReconciler = createGapReconciler({
  sync: syncDraftState,
  getUserId: getMyUserId,
  warn: (msg) => console.warn(msg),
})

setInterval(syncDraftState, DRAFT_SYNC_INTERVAL_MS)
syncDraftState() // startup: recover anything missed before this injection

async function handleFrame(frame) {
  if (!isDraftFrame(frame)) return
  // Entering a draft channel (fresh join or SPA nav into a new draft): sync the
  // full state immediately so anything missed before this moment is recovered.
  const frameDraftId = draftIdFromTopic(frame.topic)
  if (frameDraftId && frameDraftId !== knownDraftId) {
    knownDraftId = frameDraftId
    syncDraftState()
  }
  if (!format) format = detectFormat(frame)
  if (!format) return // wait for the first format-bearing frame (the join reply)

  const uid = getMyUserId()
  let result
  if (format === 'snake') {
    if (!snakeMem) snakeMem = initSnakeMemory(uid)
    result = detectSnakeEvents(snakeMem, frame)
    snakeMem = result.next
    // SNAKE gap fast-lane (additive — the resolver above still runs statusEvents
    // on this frame). Match `draft_updated_by_pick` EXACTLY: it's the once-per-pick
    // signal, distinct from auction's _by_offer/_by_nomination and from the broad
    // draft_updated* prefix. Auction is intentionally NOT wired (its autopick
    // emission is unconfirmed — separate follow-up).
    if (frame.event === 'draft_updated_by_pick') {
      gapReconciler.onPickSignal()
    }
  } else {
    if (!auctionMem) auctionMem = initAuctionMemory(uid)
    result = detectAuctionEvents(auctionMem, frame)
    auctionMem = result.next
  }

  for (const ev of result.events) {
    withClock(ev.payload)
    if (ACTIVITY_EVENTS.has(ev.type)) markDraftActive()
    try {
      await postDraftEvent(ev)
    } catch {
      // Network hiccup — drop this event, keep listening.
    }
  }
}

/** Debug capture (popup "Export captured frames") — keep the last N raw frames. */
async function captureIfEnabled(detail) {
  try {
    const store = await browser.storage.local.get(STORAGE_KEYS.CAPTURE_MODE)
    if (!store[STORAGE_KEYS.CAPTURE_MODE]) return
    const s = await browser.storage.local.get(STORAGE_KEYS.CAPTURED_FRAMES)
    const frames = s[STORAGE_KEYS.CAPTURED_FRAMES] || []
    frames.push({ url: detail.url, data: detail.data, platform: 'sleeper', ts: Date.now() })
    await browser.storage.local.set({
      [STORAGE_KEYS.CAPTURED_FRAMES]: frames.slice(-MAX_CAPTURED_FRAMES),
    })
  } catch {
    // storage hiccup — never break frame handling
  }
}

// Orphaned-context recovery now lives in ../utils/context_recovery.js, shared
// with the ESPN and Yahoo readers (#461). It was implemented here first and was
// Sleeper-only; a second copy was added for the other platforms, and two copies
// of this logic would drift. The behaviour is unchanged — the cap, the
// reset-at-startup rule and the warning are the same, and the reasoning that
// produced them is preserved in that module's header.
//
// Sleeper differs from the other readers only in WHERE the check happens: it is
// driven by WebSocket frames, so it checks on each draft frame below, while the
// polling readers check at the top of each tick.
resetContextReloadCap()

window.addEventListener('message', (e) => {
  // Only our own MAIN-world interceptor's messages, from this window.
  if (e.source !== window) return
  const d = e.data
  if (!d || d.__rook_ws__ !== true || typeof d.data !== 'string') return
  // Parse first (pure, no extension APIs) so we only act on real draft frames.
  const frame = parseFrame(d.data)
  if (!frame || !isDraftFrame(frame)) return
  if (!extensionAlive()) {
    recoverInvalidatedContext('Sleeper') // orphaned by a reload/update → reconnect
    return
  }
  noteHealthyRelay()                     // healthy relay → reset the reload cap
  captureIfEnabled(d)
  handleFrame(frame)
})

window.__rook_sleeper_poller__ = true
