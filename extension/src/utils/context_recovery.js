/**
 * Recovery from an orphaned content script — shared by every platform reader.
 *
 * WHAT GOES WRONG. When Chrome reloads or AUTO-UPDATES the extension, content
 * scripts already running in open tabs are orphaned: every `browser.*` call
 * throws "Extension context invalidated". The reader keeps running and keeps
 * parsing the page, but nothing it reads can ever reach the backend again, and
 * it says nothing about it. The draft silently stops relaying for the rest of
 * the session. The only recovery is a fresh content-script injection, which
 * means a page reload.
 *
 * WHY IT MATTERS NOW. This was written for Sleeper while the extension was
 * sideloaded, where it was close to theoretical — sideloaded extensions do not
 * auto-update. The extension is PUBLISHED on the Chrome Web Store, so Chrome
 * updates it in the background on its own schedule, including while a draft tab
 * is open. Publishing an extension update mid-draft ends the relay for every
 * ESPN and Yahoo user drafting at that moment.
 *
 * Evidence this is real: a Pro customer reported "not synced to the draft on
 * ESPN" (#461). Their draft session recorded 86 of ~160 picks, and their own
 * picks began at ROUND 7 — rounds 1-6 were never captured, and round 15 is
 * missing too. Their plan was fine and their events were being accepted, so
 * nothing refused them; the reader simply was not delivering, then started.
 *
 * THE CAP, AND WHY IT RESETS AT STARTUP. Reloading is capped so a genuinely
 * broken or disabled extension cannot put the tab in a reload loop. The cap
 * lives in sessionStorage (per tab, cleared when the tab closes). It is reset on
 * every healthy injection, which makes it per-orphaning-episode rather than
 * per-tab-lifetime: the earlier Sleeper version reset only on a healthy relay,
 * so a tab that sat in a lobby through two extension reloads exhausted the cap
 * permanently and the next orphaning only printed a warning. A reload loop is
 * not possible from the startup reset, because a disabled or broken extension
 * never injects this script at all.
 */
import browser from './browser.js'

/** How many automatic reloads to attempt per orphaning episode. */
export const MAX_CONTEXT_RELOADS = 2

const CTX_RELOAD_KEY = 'rook_ctx_reloads'

/**
 * False once the extension has been reloaded or updated and THIS content script
 * has been orphaned. Reading `browser.runtime.id` is the cheapest liveness probe
 * that does not itself throw on a healthy context.
 */
export function extensionAlive() {
  try {
    return !!(browser && browser.runtime && browser.runtime.id)
  } catch {
    return false
  }
}

function readCount() {
  try {
    return Number(sessionStorage.getItem(CTX_RELOAD_KEY) || 0)
  } catch {
    return 0     // sessionStorage blocked — fall back to the in-page attempt
  }
}

function writeCount(n) {
  try {
    sessionStorage.setItem(CTX_RELOAD_KEY, String(n))
  } catch {
    // sessionStorage blocked — the single-reload attempt still applies
  }
}

/**
 * Call once at reader startup. A fresh injection proves the context is healthy,
 * so the reload cap is cleared for the next orphaning episode.
 */
export function resetContextReloadCap() {
  if (extensionAlive()) writeCount(0)
}

/** Call whenever the reader successfully relays — the context is demonstrably alive. */
export function noteHealthyRelay() {
  writeCount(0)
}

/**
 * Reload the tab to re-inject a fresh content script, at most MAX_CONTEXT_RELOADS
 * times per episode. Past the cap it warns and gives up, leaving a manual reload
 * to the user rather than looping.
 *
 * `platformLabel` only shapes the warning text ("this ESPN tab").
 */
export function recoverInvalidatedContext(platformLabel = 'draft') {
  const n = readCount()
  if (n >= MAX_CONTEXT_RELOADS) {
    console.warn(
      `Rook: extension was reloaded/updated — refresh this ${platformLabel} tab ` +
      'to resume draft tracking.'
    )
    return false
  }
  writeCount(n + 1)
  location.reload()
  return true
}

/**
 * The one call a polling reader needs at the top of each tick.
 *
 * Returns true when the reader should STOP this tick — either the context is
 * dead and a reload was started, or it is dead and the cap is spent. Returns
 * false when the context is healthy and the tick should proceed.
 *
 * Polling readers (ESPN, Yahoo) differ from the Sleeper reader, which is driven
 * by WebSocket frames and checks on each frame instead. Both end up in the same
 * two functions above.
 */
export function guardTick(platformLabel = 'draft') {
  if (extensionAlive()) return false
  recoverInvalidatedContext(platformLabel)
  return true
}
