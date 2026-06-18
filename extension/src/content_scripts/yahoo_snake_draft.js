import browser from '../utils/browser.js'
import { postDraftEvent } from '../utils/api.js'
import { STORAGE_KEYS, DRAFT_INACTIVITY_MS } from '../utils/constants.js'
import {
  parseSnakeState,
  detectSnakeEvents,
} from './yahoo_snake_draft_observer.mjs'

/**
 * Yahoo Snake Draft Room — DOM Poller
 *
 * Reads draft state from `#app` innerText every 500ms and relays snake events
 * to the backend via POST /draft/event:
 *   - your_turn       — you're on the clock (triggers a Sonnet recommendation)
 *   - your_turn_soon  — you're 2 picks away (UI alert only)
 *   - snake_pick      — each pick made, via Yahoo's console.error ['0', ...] log
 *
 * Mirrors yahoo_draft.js (auction): pure parse/detect logic lives in
 * yahoo_snake_draft_observer.mjs (unit-tested); this file owns the DOM read,
 * the 500ms loop, and the console.error pick hook. The auction poller keys on
 * `#draft` and this one on `#app`, so both can be registered on the same URL —
 * only the matching draft type's container exists at runtime.
 */

const POLL_INTERVAL_MS = 500

let active = false
let inactivityTimer = null
let lastPickNumber = null

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
  markDraftActive()

  let memory = { wasYourTurn: false, lastPicksUntil: null }

  setInterval(async () => {
    const text = document.querySelector('#app')?.innerText
    const state = parseSnakeState(text)
    if (!state) return

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
  }, POLL_INTERVAL_MS)
}

// ---------------------------------------------------------------------------
// console.error pick interception — Yahoo logs every snake pick as
//   ['0', league, draft, pick_number, yahoo_player_id]
// ('0' = snake pick, vs 'B'=bid, 'N'=nomination for auction). Forwarded from
// the MAIN world by yahoo_draft_main.js as a '__yahoo_draft_action__' event.
// ---------------------------------------------------------------------------

window.addEventListener('__yahoo_draft_action__', async (event) => {
  const data = event.detail
  if (!Array.isArray(data) || data[0] !== '0') return

  const pickNumber = data[3]
  const yahooPlayerId = String(data[4])

  // Guard against the same frame firing twice.
  if (pickNumber === lastPickNumber) return
  lastPickNumber = pickNumber

  // Best-effort player name from the "Last:" line.
  const state = parseSnakeState(document.querySelector('#app')?.innerText)

  markDraftActive()
  try {
    await postDraftEvent({
      type: 'snake_pick',
      platform: 'yahoo',
      payload: {
        pick_number: pickNumber,
        yahoo_player_id: yahooPlayerId,
        player_name: state?.lastPick?.player_name || null,
        position: state?.lastPick?.position || null,
        picker: state?.currentPicker || 'unknown',
        round: state?.currentRound ?? null,
      },
    })
  } catch {
    // Ignore relay failures — the next poll keeps state moving
  }
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

window.__draftmind_snake__ = true
