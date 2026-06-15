/**
 * Yahoo draft — MAIN world interceptor.
 *
 * Runs in the page's MAIN world, injected by the manifest content_scripts
 * entry with "world": "MAIN" (NOT an inline <script> tag — Yahoo's CSP blocks
 * those; extension content scripts are exempt from page CSP).
 *
 * Intercepts Yahoo's own console.error draft logging and forwards the relevant
 * frames to the isolated content script (yahoo_draft.js) via a window
 * CustomEvent:
 *   ['B', league, draft, player_id, amount]  -> your bid
 *   ['N', league, draft, player_id, bid]     -> your nomination
 *
 * This only captures YOUR own actions (Yahoo logs them locally); it is a
 * nice-to-have for resolving your picks' Yahoo player IDs. The DOM poller in
 * the isolated world works independently of this file.
 */
;(function () {
  if (window.__draftmind_intercepting__) return

  const _origError = console.error
  console.error = function (...args) {
    if (
      Array.isArray(args[0]) &&
      (args[0][0] === 'B' || args[0][0] === 'N')
    ) {
      window.dispatchEvent(
        new CustomEvent('__yahoo_draft_action__', { detail: args[0] })
      )
    }
    return _origError.apply(console, args)
  }
  window.__draftmind_intercepting__ = true
})()
