import browser from './browser.js'

export async function getDraftToken() {
  const result = await browser.storage.local.get('draft_token')
  return result.draft_token || null
}

export async function postDraftEvent(event) {
  const token = await getDraftToken()
  if (!token) {
    console.debug('DraftMind: no draft token — skipping')
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
    console.debug('DraftMind: draft event failed', err)
    return false
  }
}

export function getApiBase() {
  // In production extensions, check stored preference or default to production
  // During development, default to localhost
  return 'http://localhost:8000'
}
