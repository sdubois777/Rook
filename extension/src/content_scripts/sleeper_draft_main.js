/**
 * Sleeper WebSocket interceptor — MAIN world (Phoenix Channels).
 *
 * Registered as a `world: "MAIN"` content script at document_start (NOT inline-
 * injected): Sleeper's CSP blocks page inline scripts, which is why the shared
 * `injectInterceptor()` path failed (`__rook_intercepting__` stayed undefined).
 * A world:MAIN content script is browser-injected, so CSP doesn't apply.
 *
 * Patches window.WebSocket before the page opens its draft socket and relays each
 * received frame to the ISOLATED-world poller via window.postMessage — the
 * reliable MAIN↔content channel (CustomEvent `detail` does not cross worlds in
 * Chrome). Frame data is a JSON string → structured-cloneable.
 */
;(function () {
  if (window.__rook_intercepting__) return
  const OriginalWebSocket = window.WebSocket

  class InterceptedWebSocket extends OriginalWebSocket {
    constructor(url, protocols) {
      super(url, protocols)
      this.addEventListener('message', (event) => {
        try {
          window.postMessage({ __rook_ws__: true, url, data: event.data }, '*')
        } catch {
          // non-cloneable / cross-origin — ignore, never break the page
        }
      })
    }
  }

  window.WebSocket = InterceptedWebSocket
  window.__rook_intercepting__ = true
})()
