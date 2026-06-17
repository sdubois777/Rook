/**
 * Yahoo Snake Draft Room — DOM observer (pure, testable core).
 *
 * STATUS: STUB — requires a live Yahoo snake mock-draft session to map the DOM.
 *
 * Snake draft DOM is completely different from auction:
 *   - no bid amounts, no per-player countdown clock
 *   - pick-by-pick rounds, an "on the clock" indicator
 *   - a draft board of available players
 *
 * For now this only LOGS the draft container's text so we can identify the
 * selectors needed for the real poller. Kept dependency-free (no DOM globals at
 * module load) so node:test can exercise it without a browser.
 */

// Read the draft container's text (first 500 chars) or null if not present.
export function snapshotDom(doc) {
  const el = doc.querySelector('#draft, [class*="draft"]')
  if (!el) return null
  const text = el.innerText || el.textContent || ''
  return text.substring(0, 500)
}

// Activate the observer on the given window/document. Idempotent: returns true
// if it activated this call, false if it was already running. Injectable
// win/doc/log keep it testable.
export function initSnakeObserver(win, doc, opts = {}) {
  if (win.__draftmind_snake__) return false
  win.__draftmind_snake__ = true

  const intervalMs = opts.intervalMs || 2000
  const log =
    opts.log || ((...args) => win.console && win.console.log(...args))

  log('DraftMind: snake draft observer active (STUB — DOM mapping pending)')

  let last = ''
  win.setInterval(() => {
    const snap = snapshotDom(doc)
    if (!snap || snap === last) return
    last = snap
    log('DraftMind snake DOM snapshot:', snap)
  }, intervalMs)

  return true
}
