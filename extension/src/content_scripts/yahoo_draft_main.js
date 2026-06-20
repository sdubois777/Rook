/**
 * Yahoo draft — MAIN world interceptor.
 *
 * Runs in the page's MAIN world, injected by the manifest content_scripts
 * entry with "world": "MAIN" (NOT an inline <script> tag — Yahoo's CSP blocks
 * those; extension content scripts are exempt from page CSP).
 *
 * Intercepts Yahoo's own console.error AUCTION logging and forwards your own
 * bid/nomination frames to the isolated yahoo_draft.js via a window CustomEvent:
 *   ['B', league, draft, player_id, amount] -> your bid
 *   ['N', league, draft, player_id, bid]    -> your nomination
 *
 * B/N only capture YOUR own actions (Yahoo logs them locally). Snake pick
 * frames (['0', ...]) are handled by the separate yahoo_snake_draft_main.js.
 * The DOM pollers in the isolated world work independently of this file.
 */
;(function () {
  if (window.__rook_intercepting__) return

  const FORWARDED = new Set(['B', 'N'])
  const _origError = console.error
  console.error = function (...args) {
    if (Array.isArray(args[0]) && FORWARDED.has(args[0][0])) {
      window.dispatchEvent(
        new CustomEvent('__yahoo_draft_action__', { detail: args[0] })
      )
    }
    return _origError.apply(console, args)
  }
  window.__rook_intercepting__ = true
})()
