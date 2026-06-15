import browser from '../utils/browser.js'
import { STORAGE_KEYS } from '../utils/constants.js'

document.addEventListener('DOMContentLoaded', init)

async function init() {
  const { draft_token } = await browser.storage.local.get(STORAGE_KEYS.DRAFT_TOKEN)
  if (draft_token) {
    showMainView()
  } else {
    showTokenEntry()
  }
}

/* ── Token entry screen ── */
function showTokenEntry() {
  document.getElementById('app').innerHTML = `
    <div class="header">
      <h1>DraftMind</h1>
      <div class="dot dot-red"></div>
    </div>
    <div class="section">
      <div class="label">Draft Token</div>
      <input class="input" id="token-input" placeholder="Paste from Account page" />
      <button class="btn-primary" id="save-btn">Connect</button>
    </div>
  `
  document.getElementById('save-btn').addEventListener('click', async () => {
    const token = document.getElementById('token-input').value.trim()
    if (!token) return
    await browser.storage.local.set({ [STORAGE_KEYS.DRAFT_TOKEN]: token })
    showMainView()
  })
}

/* ── Main connected view ── */
async function showMainView() {
  const platforms = await getPlatformStatus()
  const { capture_mode } = await browser.storage.local.get(STORAGE_KEYS.CAPTURE_MODE)
  const draft = await browser.storage.local.get([
    STORAGE_KEYS.ACTIVE_DRAFT,
    STORAGE_KEYS.DRAFT_PLATFORM,
  ])
  const draftActive = !!draft[STORAGE_KEYS.ACTIVE_DRAFT]
  const draftPlatform = draft[STORAGE_KEYS.DRAFT_PLATFORM] || ''

  document.getElementById('app').innerHTML = `
    <div class="header">
      <h1>DraftMind</h1>
      <div class="dot dot-green"></div>
    </div>

    <div class="token-row">
      <span class="check">Connected</span>
      <button class="btn-link" id="disconnect-btn">Disconnect</button>
    </div>

    ${
      draftActive
        ? `<div class="section">
      <div class="draft-active">🟢 Draft active — relaying ${draftPlatform} events</div>
    </div>`
        : ''
    }

    <div class="section">
      <div class="label">Platforms</div>
      ${platformRows(platforms)}
    </div>

    <div class="debug">
      <div class="debug-row">
        <span>Capture mode</span>
        <label class="toggle">
          <input type="checkbox" id="capture-toggle" ${capture_mode ? 'checked' : ''} />
          <span class="slider"></span>
        </label>
      </div>
      <button class="btn-link" id="export-btn" style="margin-top:8px">
        Export captured frames
      </button>
    </div>
  `

  document.getElementById('disconnect-btn').addEventListener('click', async () => {
    await browser.storage.local.remove(STORAGE_KEYS.DRAFT_TOKEN)
    showTokenEntry()
  })

  document.getElementById('capture-toggle').addEventListener('change', async (e) => {
    await browser.storage.local.set({ [STORAGE_KEYS.CAPTURE_MODE]: e.target.checked })
  })

  document.getElementById('export-btn').addEventListener('click', async () => {
    const { captured_frames } = await browser.storage.local.get(STORAGE_KEYS.CAPTURED_FRAMES)
    if (!captured_frames || captured_frames.length === 0) {
      alert('No captured frames yet. Enable capture mode and open a draft room.')
      return
    }
    const blob = new Blob([JSON.stringify(captured_frames, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `draftmind-frames-${Date.now()}.json`
    a.click()
    URL.revokeObjectURL(url)
  })
}

/* ── Platform status ── */
async function getPlatformStatus() {
  const keys = await browser.storage.local.get([
    STORAGE_KEYS.ESPN_CONNECTED,
    STORAGE_KEYS.YAHOO_SYNCED_AT,
    STORAGE_KEYS.SLEEPER_SYNCED_AT,
  ])
  return [
    { name: 'Yahoo', connected: !!keys[STORAGE_KEYS.YAHOO_SYNCED_AT] },
    { name: 'ESPN', connected: !!keys[STORAGE_KEYS.ESPN_CONNECTED] },
    { name: 'Sleeper', connected: !!keys[STORAGE_KEYS.SLEEPER_SYNCED_AT] },
  ]
}

function platformRows(platforms) {
  return platforms
    .map(
      (p) => `
    <div class="platform-row">
      <span>${p.name}</span>
      <span class="${p.connected ? 'badge-green' : 'badge-gray'}">
        ${p.connected ? 'Synced' : 'Not connected'}
      </span>
    </div>
  `
    )
    .join('')
}
