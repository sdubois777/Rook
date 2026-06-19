import browser from '../utils/browser.js'
import { postDraftEvent } from '../utils/api.js'
import { STORAGE_KEYS, DRAFT_INACTIVITY_MS } from '../utils/constants.js'
import {
  parseSnakeState,
  detectSnakeEvents,
  parsePicks,
} from './yahoo_snake_draft_observer.mjs'

/**
 * Yahoo Snake Draft Room — DOM Poller
 *
 * Reads draft state from `#app` innerText every 500ms and relays snake events
 * to the backend via POST /draft/event:
 *   - your_turn / your_turn_soon — turn + countdown, from parseSnakeState
 *   - snake_pick — EVERY pick (yours and opponents), parsed from the complete
 *     "Picks" panel history (parsePicks), deduped by pick number
 *
 * Pick detection reads the Picks panel rather than the single "Last:" line or
 * the console.error ['0'] frame (which only fires for your own picks and only
 * exposes a Yahoo-internal id we can't resolve). The ['0'] frame is kept ONLY
 * as a low-latency trigger to poll the panel immediately after a pick lands.
 *
 * Pure parse logic lives in yahoo_snake_draft_observer.mjs (unit-tested); this
 * file owns the DOM read and the loop. The auction poller keys on `#draft` and
 * this one on `#app`, so both can be registered on the same URL.
 */

const POLL_INTERVAL_MS = 500
const TOTAL_TEAMS = 12 // round = ceil(pick_number / TOTAL_TEAMS); 12 for our leagues

let active = false
let inactivityTimer = null

// Pick numbers already relayed — the panel holds the FULL history, so we only
// post picks we haven't sent yet (each pick number is unique).
const sentPickNumbers = new Set()

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

/** Parse the Picks panel and relay any picks we haven't sent yet. */
async function pollPicksPanel() {
  const text = document.querySelector('#app')?.innerText
  if (!text) return
  for (const pick of parsePicks(text)) {
    if (sentPickNumbers.has(pick.pick_number)) continue
    sentPickNumbers.add(pick.pick_number)
    markDraftActive()
    try {
      await postDraftEvent({
        type: 'snake_pick',
        platform: 'yahoo',
        payload: {
          pick_number: pick.pick_number,
          player_name: pick.player_name,
          position: pick.position,
          nfl_team: pick.nfl_team,
          picker: pick.team,
          is_yours: pick.is_yours,
          round: Math.ceil(pick.pick_number / TOTAL_TEAMS),
        },
      })
    } catch {
      // Network hiccup — forget so the next poll retries this pick.
      sentPickNumbers.delete(pick.pick_number)
    }
  }
}

function startPoller() {
  if (active) return
  active = true
  markDraftActive()

  let memory = { wasYourTurn: false, lastPicksUntil: null }

  setInterval(async () => {
    const text = document.querySelector('#app')?.innerText
    const state = parseSnakeState(text)
    if (state) {
      const { events, next } = detectSnakeEvents(memory, state)
      memory = next
      for (const event of events) {
        if (event.type === 'your_turn') markDraftActive()
        try {
          await postDraftEvent(event)
        } catch {
          // Network hiccup — drop this event, keep polling
        }
      }
    }
    await pollPicksPanel()
  }, POLL_INTERVAL_MS)
}

// ---------------------------------------------------------------------------
// ['0'] frame — Yahoo logs a console.error ['0', ...] when a pick lands. The
// MAIN-world script (yahoo_snake_draft_main.js) forwards it as a content-free
// '__yahoo_pick_made__' trigger; we use it only to poll the Picks panel
// IMMEDIATELY (lower latency than waiting for the next 500ms tick). The frame's
// own data is NOT used — the Picks panel is the source of truth.
// ---------------------------------------------------------------------------

window.addEventListener('__yahoo_pick_made__', () => {
  pollPicksPanel()
})

// ---------------------------------------------------------------------------
// Bootstrap — wait for the snake room (#app) to render, then start
// ---------------------------------------------------------------------------

function bootstrap() {
  if (document.querySelector('#app')) {
    startPoller()
    return
  }
  const observer = new MutationObserver(() => {
    if (document.querySelector('#app')) {
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

// Presence flag for the ISOLATED-world poller. The page-detectable
// __draftmind_snake__ flag is set by yahoo_snake_draft_main.js in the MAIN world.
window.__draftmind_snake_poller__ = true
