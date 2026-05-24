/**
 * Patches window.WebSocket to intercept all WS frames on the page.
 *
 * Dispatches CustomEvent '__draftmind_ws_frame__' for each message received.
 *
 * MUST run in MAIN world — inject via script tag, not directly from content script.
 */
;(function () {
  if (window.__draftmind_intercepting__) return

  const OriginalWebSocket = window.WebSocket

  class InterceptedWebSocket extends OriginalWebSocket {
    constructor(url, protocols) {
      super(url, protocols)
      this.addEventListener('message', (event) => {
        window.dispatchEvent(
          new CustomEvent('__draftmind_ws_frame__', {
            detail: { url, data: event.data },
          })
        )
      })
    }
  }

  window.WebSocket = InterceptedWebSocket
  window.__draftmind_intercepting__ = true
})()
