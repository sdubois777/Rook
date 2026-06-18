/**
 * Yahoo draft — MAIN world interceptor.
 *
 * Runs in the page's MAIN world, injected by the manifest content_scripts
 * entry with "world": "MAIN" (NOT an inline <script> tag — Yahoo's CSP blocks
 * those; extension content scripts are exempt from page CSP).
 *
 * Intercepts Yahoo's own console.error draft logging and forwards the relevant
 * frames to the isolated content scripts (yahoo_draft.js / yahoo_snake_draft.js)
 * via a window CustomEvent:
 *   ['B', league, draft, player_id, amount]      -> your bid       (auction)
 *   ['N', league, draft, player_id, bid]         -> your nomination (auction)
 *   ['0', league, draft, pick_number, player_id] -> a pick made    (snake)
 *
 * B/N only capture YOUR own actions (Yahoo logs them locally); '0' fires for
 * every snake pick. The DOM pollers in the isolated world work independently of
 * this file.
 */
;(function () {
  if (window.__draftmind_intercepting__) return

  const FORWARDED = new Set(['B', 'N', '0'])
  const _origError = console.error
  console.error = function (...args) {
    if (Array.isArray(args[0]) && FORWARDED.has(args[0][0])) {
      window.dispatchEvent(
        new CustomEvent('__yahoo_draft_action__', { detail: args[0] })
      )
    }
    return _origError.apply(console, args)
  }
  window.__draftmind_intercepting__ = true
})()
