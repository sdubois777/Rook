import browser from '../utils/browser.js'
import { postDraftEvent } from '../utils/api.js'
import { STORAGE_KEYS, DRAFT_INACTIVITY_MS } from '../utils/constants.js'
import { stripDarkreader } from './espn_shared.mjs'
import {
  isSalaryCap,
  resolveSalaryCapState,
  detectSalaryCapEvents,
  initSalaryCapMemory,
} from './espn_salarycap_resolve.mjs'
import {
  isSnake,
  resolveSnakeState,
  detectSnakeEvents,
  initSnakeMemory,
} from './espn_snake_resolve.mjs'

/**
 * ESPN Draft Room poller (React + styled-jsx, content-gated).
 *
 * Replaces the old WS-frame stub. ESPN has no stable root id, so we gate on
 * CONTENT (testid-driven), never on a root:
 *   - salary cap  → `[data-testid="auction-pick"]` / `bidding-form`
 *   - snake       → `[data-testid="current-pick"]` and NO `auction-pick`
 * Reads the React DOM non-destructively each tick and relays the existing draft
 * event contract (nomination/bid_update/clock/draft_pick/teams_update for salary
 * cap; your_turn/your_turn_soon/snake_status/snake_pick for snake) via
 * POST /draft/event. Pure parse/diff lives in the resolver modules (unit-tested);
 * this file owns the DOM reads and the loop.
 *
 * Cross-poller safety: zero Yahoo `ys-*` hooks here, and the manifest host-split
 * keeps this inert on Yahoo while the Yahoo pollers stay inert on ESPN.
 */

const POLL_INTERVAL_MS = 500
const DETECT_INTERVAL_MS = 1000

let active = false
let format = null // 'salarycap' | 'snake'
let memory = null
let inactivityTimer = null

function markDraftActive() {
  browser.storage.local.set({
    [STORAGE_KEYS.ACTIVE_DRAFT]: true,
    [STORAGE_KEYS.DRAFT_PLATFORM]: 'espn',
  })
  if (inactivityTimer) clearTimeout(inactivityTimer)
  inactivityTimer = setTimeout(() => {
    browser.storage.local.set({ [STORAGE_KEYS.ACTIVE_DRAFT]: false })
  }, DRAFT_INACTIVITY_MS)
}

/** Which format is live right now (content-based), or null. */
function detectFormat(root) {
  if (isSalaryCap(root)) return 'salarycap'
  if (isSnake(root)) return 'snake'
  return null
}

async function tick() {
  const root = stripDarkreader(document)
  // Format can only be confirmed once; if the gate goes quiet (between states)
  // keep the locked format so we don't thrash.
  const fmt = format || detectFormat(root)
  if (!fmt) return
  const { events, next } =
    fmt === 'salarycap'
      ? detectSalaryCapEvents(memory, resolveSalaryCapState(root))
      : detectSnakeEvents(memory, resolveSnakeState(root))
  memory = next
  for (const event of events) {
    if (event.type === 'your_turn' || event.type === 'snake_pick' ||
        event.type === 'nomination' || event.type === 'draft_pick') {
      markDraftActive()
    }
    try {
      await postDraftEvent(event)
    } catch {
      // Network hiccup — drop this event, keep polling.
    }
  }
}

function startPoller(fmt) {
  if (active) return
  active = true
  format = fmt
  memory = fmt === 'salarycap' ? initSalaryCapMemory() : initSnakeMemory()
  markDraftActive()
  setInterval(tick, POLL_INTERVAL_MS)
}

// ---------------------------------------------------------------------------
// Bootstrap — wait for positive ESPN draft content, then start the read-only
// poller. No page-mutating action is ever taken.
// ---------------------------------------------------------------------------
function ready() {
  return detectFormat(document)
}

function waitForDraft() {
  const fmt = ready()
  if (fmt) {
    startPoller(fmt)
    return
  }
  setTimeout(waitForDraft, DETECT_INTERVAL_MS)
}

function bootstrap() {
  const fmt = ready()
  if (fmt) {
    startPoller(fmt)
    return
  }
  const observer = new MutationObserver(() => {
    const f = ready()
    if (f) {
      observer.disconnect()
      startPoller(f)
    }
  })
  observer.observe(document.documentElement, { childList: true, subtree: true })
  waitForDraft()
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootstrap)
} else {
  bootstrap()
}

window.__rook_espn_poller__ = true
