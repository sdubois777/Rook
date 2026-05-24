import browser from '../utils/browser.js'
import INTERCEPTOR_CODE from '../utils/ws_interceptor.js?raw'

// Inject WS interceptor into page world
;(function injectInterceptor() {
  if (window.__draftmind_intercepting__) return
  const script = document.createElement('script')
  script.textContent = INTERCEPTOR_CODE
  ;(document.head || document.documentElement).appendChild(script)
  script.remove()
})()

window.addEventListener('__draftmind_ws_frame__', async (event) => {
  const { capturing } = await browser.storage.local.get('capturing')
  if (capturing) {
    await captureFrame(event.detail)
  }

  const parsed = parseESPNFrame(event.detail.data)
  if (!parsed) return

  browser.runtime.sendMessage({
    type: 'DRAFT_EVENT',
    payload: { ...parsed, platform: 'espn' },
  })
})

function parseESPNFrame(data) {
  // STUB — real format TBD after frame capture
  try {
    JSON.parse(data)
    return null
  } catch {
    return null
  }
}

async function captureFrame(detail) {
  const { captured_frames = [] } = await browser.storage.local.get('captured_frames')
  captured_frames.push({
    ...detail,
    platform: 'espn',
    ts: Date.now(),
  })
  await browser.storage.local.set({
    captured_frames: captured_frames.slice(-50),
  })
}
