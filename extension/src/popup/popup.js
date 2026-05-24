import browser from '../utils/browser.js'
import { getApiBase } from '../utils/api.js'

document.addEventListener('DOMContentLoaded', init)

async function init() {
  const { draft_token } = await browser.storage.local.get('draft_token')
  if (draft_token) {
    showMainView(draft_token)
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
    await browser.storage.local.set({ draft_token: token })
    showMainView(token)
  })
}

/* ── Main connected view ── */
async function showMainView(token) {
  const platforms = await getPlatformStatus()
  const { capture_mode } = await browser.storage.local.get('capture_mode')

  document.getElementById('app').innerHTML = `
    <div class="header">
      <h1>DraftMind</h1>
      <div class="dot dot-green"></div>
    </div>

    <div class="token-row">
      <span class="check">Connected</span>
      <button class="btn-link" id="disconnect-btn">Disconnect</button>
    </div>

    <div class="section">
      <div class="label">Platforms</div>
      ${platformRows(platforms)}
    </div>

    <div id="draft-status"></div>

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
    await browser.storage.local.remove('draft_token')
    showTokenEntry()
  })

  document.getElementById('capture-toggle').addEventListener('change', async (e) => {
    await browser.storage.local.set({ capture_mode: e.target.checked })
  })

  document.getElementById('export-btn').addEventListener('click', async () => {
    const { captured_frames } = await browser.storage.local.get('captured_frames')
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

  checkDraftStatus(token)
}

/* ── Platform status ── */
async function getPlatformStatus() {
  const keys = await browser.storage.local.get([
    'espn_connected', 'yahoo_synced_at', 'sleeper_synced_at',
  ])
  return [
    { name: 'Yahoo', connected: !!keys.yahoo_synced_at },
    { name: 'ESPN', connected: !!keys.espn_connected },
    { name: 'Sleeper', connected: !!keys.sleeper_synced_at },
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

/* ── Draft status check ── */
async function checkDraftStatus(token) {
  try {
    const resp = await fetch(`${getApiBase()}/draft/active`, {
      headers: { 'X-Draft-Token': token },
    })
    if (resp.ok) {
      const data = await resp.json()
      if (data.active) {
        document.getElementById('draft-status').innerHTML = `
          <div class="draft-active">
            <div class="pulse"></div>
            Live Draft — ${data.platform || 'Unknown'}
          </div>
        `
      }
    }
  } catch {
    // Backend unreachable — ignore silently
  }
}
