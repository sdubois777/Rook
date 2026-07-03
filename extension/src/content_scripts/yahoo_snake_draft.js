import browser from '../utils/browser.js'
import { postDraftEvent } from '../utils/api.js'
import { STORAGE_KEYS, DRAFT_INACTIVITY_MS } from '../utils/constants.js'
import {
  snakeRoot,
  shouldSnakeActivate,
  resolveSnakeState,
  detectSnakeEvents,
  initSnakeMemory,
} from './yahoo_snake_resolve.mjs'

/**
 * Yahoo Snake Draft Room — React DOM poller (2026 replatform).
 *
 * Yahoo moved the snake room onto the SAME React root as auction
 * (`#main-0-DraftClientBootstrap-Proxy`). The old `#app`-innerText + pick-card
 * poller (and its destructive "Picks"-tab click) is dead. This reads the React
 * Board view structurally and NON-DESTRUCTIVELY — no tab clicks — relaying:
 *   - your_turn / your_turn_soon — turn + countdown, from the turn banner
 *   - snake_status — current pick/round + countdown, on change
 *   - snake_pick — EVERY pick, from the "Last:" indicator + serpentine board,
 *     deduped by pick number
 *
 * Cross-poller safety is CONTENT-based (see yahoo_snake_resolve.mjs): this poller
 * activates ONLY on the snake turn banner; the auction poller activates only on a
 * Proj-$ nominee / $-budget team cards. The shared root is harmless — neither
 * vetoes on the root's mere presence (the old auction-root veto is retired).
 *
 * Pure parse/diff logic lives in yahoo_snake_resolve.mjs (unit-tested); this file
 * owns the DOM reads and the loop.
 */

const POLL_INTERVAL_MS = 500
const SNAKE_DETECT_INTERVAL_MS = 1000

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

let memory = initSnakeMemory()

// Relay queue: detection is synchronous (per tick), but posting awaits the
// network — serialize posts in DETECTION order so a mutation-driven tick can't
// interleave its events ahead of an earlier tick's still-in-flight ones (the
// backend must record a pick before the your_turn that follows it).
let postQueue = Promise.resolve()

/** One non-destructive read of the React board → relay any new events. */
function tick() {
  const root = snakeRoot(document)
  if (!root) return
  const state = resolveSnakeState(root)
  const { events, next } = detectSnakeEvents(memory, state)
  // LOUD gap alarm: a skipped pick number means a pick was never captured (the
  // Board view only ever shows the single latest pick — nothing can backfill
  // it). Surface it instead of losing it silently.
  if (next.missedPickNumbers && next.missedPickNumbers.length) {
    console.warn(
      `Rook snake: MISSED pick(s) #${next.missedPickNumbers.join(', #')} — ` +
        'not captured from the board (only the latest pick is rendered)'
    )
  }
  memory = next
  for (const event of events) {
    if (event.type === 'your_turn' || event.type === 'snake_pick') markDraftActive()
    postQueue = postQueue.then(() =>
      postDraftEvent(event).catch(() => {
        // Network hiccup — drop this event, keep polling.
      })
    )
  }
}

function startPoller() {
  if (active) return
  active = true
  markDraftActive()
  setInterval(tick, POLL_INTERVAL_MS)

  // MUTATION-DRIVEN READS — the dropped-pick fix. The Board view renders only
  // ONE completed pick (the "Last:" indicator), so two picks landing inside a
  // single 500ms poll window meant the earlier one was never the "Last:" value
  // at any read → its snake_pick never fired, and (delta-only capture) nothing
  // could ever backfill it. React commits each pick update separately, so
  // observing the root and reading on EVERY mutation batch sees each "Last:"
  // value in turn — including back-to-back autopicks at the snake turn
  // boundary. tick() is cheap (three span scans) and detectSnakeEvents dedupes
  // by pick number, so redundant reads are no-ops. The 500ms interval stays as
  // a fallback for anything an observer miss could drop.
  const root = snakeRoot(document)
  if (root) {
    new MutationObserver(() => tick()).observe(root, {
      childList: true,
      subtree: true,
      characterData: true,
    })
  }
}

// ---------------------------------------------------------------------------
// ['0'] frame — Yahoo logs a console.error ['0', ...] when a pick lands. The
// MAIN-world script (yahoo_snake_draft_main.js) forwards it as a content-free
// '__yahoo_pick_made__' trigger; we use it only to read the board IMMEDIATELY
// (lower latency than waiting for the next 500ms tick). The frame's own data is
// NOT used — the React board is the source of truth.
// ---------------------------------------------------------------------------
window.addEventListener('__yahoo_pick_made__', () => {
  if (active) tick()
})

// ---------------------------------------------------------------------------
// Bootstrap — auction and snake share the React root + URL match patterns, so we
// POSITIVELY confirm a snake draft (the turn banner) before activating. No
// page-mutating action is ever taken, so there is nothing to gate destructively;
// we simply wait for snake content to render, then start the read-only poller.
// ---------------------------------------------------------------------------
function snakeReady() {
  return shouldSnakeActivate(snakeRoot(document))
}

function waitForSnakeDraft() {
  if (snakeReady()) {
    startPoller()
    return
  }
  setTimeout(waitForSnakeDraft, SNAKE_DETECT_INTERVAL_MS)
}

function bootstrap() {
  if (snakeReady()) {
    startPoller()
    return
  }
  // Snake content may render after initial load — watch for it, but also keep a
  // coarse interval as a fallback in case the observer misses a subtree swap.
  const observer = new MutationObserver(() => {
    if (snakeReady()) {
      observer.disconnect()
      startPoller()
    }
  })
  observer.observe(document.documentElement, { childList: true, subtree: true })
  waitForSnakeDraft()
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootstrap)
} else {
  bootstrap()
}

// Presence flag for the ISOLATED-world poller. The page-detectable
// __rook_snake__ flag is set by yahoo_snake_draft_main.js in the MAIN world.
window.__rook_snake_poller__ = true
