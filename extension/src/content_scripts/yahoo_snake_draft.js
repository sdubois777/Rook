import browser from '../utils/browser.js'
import { postDraftEvent } from '../utils/api.js'
import { STORAGE_KEYS, DRAFT_INACTIVITY_MS } from '../utils/constants.js'
import {
  parseSnakeState,
  detectSnakeEvents,
  parsePickCards,
  findPicksButton,
  shouldSnakeActivate,
} from './yahoo_snake_draft_observer.mjs'
import { AUCTION_ROOT_SELECTOR } from './yahoo_auction_resolve.mjs'

/**
 * Yahoo Snake Draft Room — DOM Poller
 *
 * Relays snake events to the backend via POST /draft/event:
 *   - your_turn / your_turn_soon — turn + countdown, from parseSnakeState
 *   - snake_pick — EVERY pick (yours and opponents), read from the pick CARDS
 *     via a CSS selector (parsePickCards), deduped by pick number
 *
 * Pick cards are targeted with a CSS selector instead of scanning #app
 * innerText: the panel text is full of other integers (expert ranks, stat
 * columns) that confused the old text parser. The selector hits the cards
 * directly. A MutationObserver on the scroll container fires the moment a card
 * is appended; the 500ms interval and the ['0'] console frame are fallbacks.
 *
 * Pure parse logic lives in yahoo_snake_draft_observer.mjs (unit-tested); this
 * file owns the DOM reads and the loop.
 */

const POLL_INTERVAL_MS = 500
const TOTAL_TEAMS = 12 // round = ceil(pick_number / TOTAL_TEAMS); 12 for our leagues

// Each pick is a card with this exact stack of Yahoo atomic classes (confirmed
// live — 93 picks). The selector escapes (, ), and -- for querySelectorAll.
const PICK_CARD_SELECTOR =
  '.D\\(f\\).Fld\\(r\\).Ai\\(c\\).Gp\\(8px\\).Bdrs\\(8px\\).P\\(12px\\)' +
  '.Bgc\\(--ys-colors-surface-accent\\)'

// The scroll container the cards are appended to (for the MutationObserver).
const PICK_CONTAINER_SELECTOR =
  '.Fxg\\(1\\).Ovy\\(a\\).Ovx\\(h\\).D\\(f\\).Fxd\\(c\\).Gp\\(4px\\).Pb\\(16px\\)'

let active = false
let inactivityTimer = null

// Pick numbers already relayed — the panel holds the FULL history, so we only
// post picks we haven't sent yet (each pick number is unique).
const sentPickNumbers = new Set()

/** Read every pick card from the DOM and parse it (DOM read + pure parse). */
function getAllPicks() {
  const cards = document.querySelectorAll(PICK_CARD_SELECTOR)
  return parsePickCards(Array.from(cards, (el) => el.innerText))
}

/**
 * The pick cards only render while the "Picks" tab is active. Click it.
 * Returns true if the button was found and clicked.
 */
function clickPicksTab() {
  const btn = findPicksButton(Array.from(document.querySelectorAll('button')))
  if (btn) {
    btn.click()
    return true
  }
  return false
}

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

/** Read the pick cards and relay any picks we haven't sent yet. */
async function pollPicksPanel() {
  const picks = getAllPicks()

  // 0 cards but we've already seen picks means the user switched away from the
  // Picks tab — re-click it and let the next poll read the re-rendered cards.
  if (picks.length === 0 && sentPickNumbers.size > 0) {
    clickPicksTab()
    return
  }

  for (const pick of picks) {
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

  startPicksObserver()
}

// A MutationObserver on the picks scroll container fires the instant a new card
// is appended — no polling delay. The 500ms interval is the fallback. Retries
// until the container exists (it renders after the room loads).
function startPicksObserver() {
  const container = document.querySelector(PICK_CONTAINER_SELECTOR)
  if (!container) {
    setTimeout(startPicksObserver, 2000)
    return
  }
  const observer = new MutationObserver(() => {
    pollPicksPanel()
  })
  observer.observe(container, { childList: true, subtree: false })
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
// Bootstrap — wait for the snake room (#app), POSITIVELY confirm it's a snake
// draft, click the Picks tab so the pick cards render, then start polling.
// ---------------------------------------------------------------------------

const SNAKE_DETECT_INTERVAL_MS = 1000

// Click the Picks tab (retrying until the button exists), then start the poller
// once the cards have had a moment to render.
function startWhenPicksReady() {
  if (clickPicksTab()) {
    setTimeout(startPoller, 500)
  } else {
    setTimeout(startWhenPicksReady, 1000)
  }
}

// Auction rooms share our URL match patterns and ALSO have #app, so we must
// confirm a snake draft BEFORE any page-mutating action. The clickPicksTab() in
// startWhenPicksReady switches the view; on an auction room it starves the
// auction poller. Bail permanently the moment an auction room is detected —
// legacy #draft OR the React auction root (#draft was removed by Yahoo's 2026
// replatform) — otherwise wait for snake markers to render, then start. NEVER
// click on a page we haven't positively identified as snake.
function waitForSnakeDraft() {
  const hasDraftPanel = !!document.querySelector('#draft')
  const hasAuctionRoot = !!document.querySelector(AUCTION_ROOT_SELECTOR)
  if (hasDraftPanel || hasAuctionRoot) return // auction room — never act
  const appText = document.querySelector('#app')?.innerText || ''
  if (shouldSnakeActivate({ hasDraftPanel, hasAuctionRoot, appText })) {
    startWhenPicksReady()
    return
  }
  setTimeout(waitForSnakeDraft, SNAKE_DETECT_INTERVAL_MS)
}

function bootstrap() {
  if (document.querySelector('#app')) {
    waitForSnakeDraft()
    return
  }
  const observer = new MutationObserver(() => {
    if (document.querySelector('#app')) {
      observer.disconnect()
      waitForSnakeDraft()
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
// __rook_snake__ flag is set by yahoo_snake_draft_main.js in the MAIN world.
window.__rook_snake_poller__ = true
