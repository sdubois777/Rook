import browser from '../utils/browser.js'
import { postDraftEvent, getApiBase, getDraftToken } from '../utils/api.js'
import { MESSAGE_TYPES } from '../utils/constants.js'

browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === MESSAGE_TYPES.DRAFT_EVENT) {
    postDraftEvent(message.payload)
      .then((ok) => sendResponse({ ok }))
      .catch((err) => sendResponse({ ok: false, error: err.message }))
    return true // keep channel open for async response
  }

  if (message.type === MESSAGE_TYPES.ESPN_COOKIES) {
    sendESPNCookies(message.payload)
      .then((result) => sendResponse(result))
      .catch((err) => sendResponse({ ok: false, error: err.message }))
    return true
  }
})

async function sendESPNCookies(payload) {
  const draft_token = await getDraftToken()
  if (!draft_token) {
    throw new Error('No draft token — set in extension popup')
  }

  const params = new URLSearchParams({
    espn_s2: payload.espn_s2,
    swid: payload.swid,
    ...(payload.league_id ? { league_id: payload.league_id } : {}),
  })

  const resp = await fetch(
    `${getApiBase()}/leagues/connect/espn/callback?${params}`,
    {
      method: 'GET',
      headers: { 'X-Draft-Token': draft_token },
    }
  )
  if (!resp.ok) throw new Error(await resp.text())
  return { ok: true }
}
