import browser from './browser.js'
import { STORAGE_KEYS } from './constants.js'

export async function getDraftToken() {
  const result = await browser.storage.local.get(STORAGE_KEYS.DRAFT_TOKEN)
  return result[STORAGE_KEYS.DRAFT_TOKEN] || null
}

export async function postDraftEvent(event) {
  let token
  try {
    token = await getDraftToken()
  } catch {
    // Extension context invalidated (reload/update orphaned this content script)
    // or storage unavailable — never let it reject uncaught; the poller's
    // context check handles recovery.
    return false
  }
  if (!token) {
    console.debug('Rook: no draft token — skipping')
    return false
  }
  try {
    const resp = await fetch(`${getApiBase()}/draft/event`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Draft-Token': token,
      },
      body: JSON.stringify(event),
    })
    return resp.ok
  } catch (err) {
    console.debug('Rook: draft event failed', err)
    return false
  }
}

export function getApiBase() {
  // Extension always talks to production. There is no "dev mode" for an
  // extension running in a real browser against real Yahoo/ESPN pages. The API
  // is served under /api (the root namespace is the SPA), so endpoints are
  // /api/draft/event etc. (Host stays the Railway URL until rookff.com DNS
  // is live.)
  return 'https://fantasymanager-production.up.railway.app/api'
}
