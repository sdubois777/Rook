/**
 * Shared draft content-script plumbing.
 *
 * Every platform draft script (yahoo/espn/sleeper) does the same three
 * things: inject the WS interceptor into the page world, optionally
 * capture raw frames for debugging, and relay parsed draft events to
 * the service worker. Only the frame parser differs per platform —
 * this module owns everything else.
 */
import browser from './browser.js'
import INTERCEPTOR_CODE from './ws_interceptor.js?raw'
import {
  MAX_CAPTURED_FRAMES,
  MESSAGE_TYPES,
  STORAGE_KEYS,
  WS_FRAME_EVENT,
} from './constants.js'

/** Inject the WS interceptor into the page (MAIN) world. Idempotent. */
export function injectInterceptor() {
  if (window.__draftmind_intercepting__) return
  const script = document.createElement('script')
  script.textContent = INTERCEPTOR_CODE
  ;(document.head || document.documentElement).appendChild(script)
  script.remove()
}

/**
 * Listen for intercepted WS frames; capture them when capture mode is
 * on, and relay frames `parseFrame` understands to the service worker.
 *
 * @param {string} platform  'yahoo' | 'espn' | 'sleeper'
 * @param {(data: string) => object | null} parseFrame  platform parser;
 *   returns null for frames that are not draft events.
 */
export function listenForFrames(platform, parseFrame) {
  window.addEventListener(WS_FRAME_EVENT, async (event) => {
    const store = await browser.storage.local.get(STORAGE_KEYS.CAPTURE_MODE)
    if (store[STORAGE_KEYS.CAPTURE_MODE]) {
      await captureFrame(platform, event.detail)
    }

    const parsed = parseFrame(event.detail.data)
    if (!parsed) return

    try {
      await browser.runtime.sendMessage({
        type: MESSAGE_TYPES.DRAFT_EVENT,
        payload: { ...parsed, platform },
      })
    } catch (err) {
      // Service worker may be asleep or the extension reloading —
      // log for debugging, never break the host page.
      console.debug(`DraftMind: draft event relay failed (${platform})`, err)
    }
  })
}

/** Append one raw frame to the capture buffer, keeping the newest N. */
async function captureFrame(platform, detail) {
  const store = await browser.storage.local.get(STORAGE_KEYS.CAPTURED_FRAMES)
  const frames = store[STORAGE_KEYS.CAPTURED_FRAMES] || []
  frames.push({ ...detail, platform, ts: Date.now() })
  await browser.storage.local.set({
    [STORAGE_KEYS.CAPTURED_FRAMES]: frames.slice(-MAX_CAPTURED_FRAMES),
  })
}
