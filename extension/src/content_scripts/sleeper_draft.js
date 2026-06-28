import browser from '../utils/browser.js'
import { postDraftEvent } from '../utils/api.js'
import {
  STORAGE_KEYS,
  DRAFT_INACTIVITY_MS,
  MAX_CAPTURED_FRAMES,
} from '../utils/constants.js'
import { parseFrame, isDraftFrame, parseUserId } from './sleeper_resolve.mjs'
import { initSnakeMemory, detectSnakeEvents } from './sleeper_snake_resolve.mjs'
import { initAuctionMemory, detectAuctionEvents } from './sleeper_auction_resolve.mjs'

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

const ACTIVITY_EVENTS = new Set(['your_turn', 'snake_pick', 'nomination', 'draft_pick'])

async function handleFrame(frame) {
  if (!isDraftFrame(frame)) return
  if (!format) format = detectFormat(frame)
  if (!format) return // wait for the first format-bearing frame (the join reply)

  const uid = getMyUserId()
  let result
  if (format === 'snake') {
    if (!snakeMem) snakeMem = initSnakeMemory(uid)
    result = detectSnakeEvents(snakeMem, frame)
    snakeMem = result.next
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

/** False once the extension is reloaded/updated and this content script orphaned. */
function extensionAlive() {
  try {
    return !!(browser && browser.runtime && browser.runtime.id)
  } catch {
    return false
  }
}

/**
 * When the extension is reloaded or AUTO-UPDATED, content scripts already running
 * in open tabs are orphaned: their browser.* calls throw "Extension context
 * invalidated" and relaying silently dies (the MAIN-world interceptor keeps posting
 * frames, but nothing reaches the backend). The only recovery is a fresh content-
 * script injection = a page reload. Auto-reload once (capped to avoid a loop if the
 * extension is disabled, reset on the next healthy relay) so a LIVE draft survives
 * an extension update without the user noticing.
 */
const CTX_RELOAD_KEY = 'rook_ctx_reloads'
function recoverInvalidatedContext() {
  try {
    const n = Number(sessionStorage.getItem(CTX_RELOAD_KEY) || 0)
    if (n >= 2) {
      console.warn(
        'Rook: extension was reloaded/updated — refresh this Sleeper tab to resume draft tracking.'
      )
      return
    }
    sessionStorage.setItem(CTX_RELOAD_KEY, String(n + 1))
  } catch {
    // sessionStorage blocked — still attempt a single reload
  }
  location.reload()
}

window.addEventListener('message', (e) => {
  // Only our own MAIN-world interceptor's messages, from this window.
  if (e.source !== window) return
  const d = e.data
  if (!d || d.__rook_ws__ !== true || typeof d.data !== 'string') return
  // Parse first (pure, no extension APIs) so we only act on real draft frames.
  const frame = parseFrame(d.data)
  if (!frame || !isDraftFrame(frame)) return
  if (!extensionAlive()) {
    recoverInvalidatedContext() // orphaned by an extension reload/update → reconnect
    return
  }
  try {
    sessionStorage.setItem(CTX_RELOAD_KEY, '0') // healthy relay → reset the reload cap
  } catch {
    // ignore
  }
  captureIfEnabled(d)
  handleFrame(frame)
})

window.__rook_sleeper_poller__ = true
