import browser from '../utils/browser.js'
import { postDraftEvent } from '../utils/api.js'
import { STORAGE_KEYS, DRAFT_INACTIVITY_MS } from '../utils/constants.js'
import { parseDraftState, detectEvents } from './yahoo_draft_parse.mjs'

/**
 * Yahoo Draft Room DOM Poller
 *
 * Reads draft state from `#draft` innerText every 300ms and relays
 * nominations, bid updates, clock ticks, sold picks, and budget changes
 * to the backend via POST /draft/event. Also intercepts Yahoo's own
 * console.error draft logging (['B',...] / ['N',...]) to capture the
 * Yahoo player IDs of YOUR bids and nominations.
 *
 * Does NOT use WS interception — Yahoo's CSP blocks content-script
 * injection, so DOM polling + a main-world console.error hook is the
 * reliable alternative. The pure parsing/detection logic lives in
 * yahoo_draft_parse.mjs (unit-tested); this file is the browser glue.
 */

const POLL_INTERVAL_MS = 300

// ---------------------------------------------------------------------------
// Main-world injection — intercept Yahoo's console.error draft events
// ---------------------------------------------------------------------------

function injectMainWorldScript() {
  const script = document.createElement('script')
  script.textContent = `
    (function () {
      const _origError = console.error
      console.error = function (...args) {
        if (Array.isArray(args[0]) && (args[0][0] === 'B' || args[0][0] === 'N')) {
          window.dispatchEvent(
            new CustomEvent('__yahoo_draft_action__', { detail: args[0] })
          )
        }
        return _origError.apply(console, args)
      }
      window.__draftmind_intercepting__ = true
    })();
  `
  ;(document.head || document.documentElement).appendChild(script)
  script.remove()
}

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
  markDraftActive()

  let memory = {
    lastPlayer: null,
    lastBid: null,
    lastClock: null,
    prevTeams: {},
  }

  setInterval(async () => {
    const text = document.querySelector('#draft')?.innerText
    const state = parseDraftState(text)
    if (!state) return

    const { events, next } = detectEvents(memory, state)
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
// console.error listener — YOUR own bids/nominations (carry Yahoo player IDs)
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
// Bootstrap — wait for the draft room to render, then start
// ---------------------------------------------------------------------------

injectMainWorldScript()

function bootstrap() {
  if (document.querySelector('#draft')) {
    startPoller()
    return
  }
  const observer = new MutationObserver(() => {
    if (document.querySelector('#draft')) {
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

// Signal extension presence to the page
window.__draftmind__ = true
