import browser from '../utils/browser.js'
import { postDraftEvent, getApiBase, getDraftToken } from '../utils/api.js'
import { MESSAGE_TYPES } from '../utils/constants.js'

// The origin ESPN scopes espn_s2 / SWID to. cookies.get keys on the URL.
const ESPN_COOKIE_URL = 'https://fantasy.espn.com'

browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === MESSAGE_TYPES.DRAFT_EVENT) {
    postDraftEvent(message.payload)
      .then((ok) => sendResponse({ ok }))
      .catch((err) => sendResponse({ ok: false, error: err.message }))
    return true // keep channel open for async response
  }

  if (message.type === MESSAGE_TYPES.ESPN_COOKIES) {
    connectEspnFromCookies(message.payload)
      .then((result) => sendResponse(result))
      .catch((err) => sendResponse({ ok: false, error: err.message }))
    return true
  }
})

/**
 * Read one ESPN cookie by name via the cookies API. Unlike document.cookie
 * (which the content script can't see for httpOnly cookies — the old bug), the
 * service-worker cookies API reads httpOnly cookies too. Scoped strictly by
 * name: only espn_s2 / SWID are ever requested — no other espn.com cookie.
 */
async function readEspnCookie(name) {
  const cookie = await browser.cookies.get({ url: ESPN_COOKIE_URL, name })
  return cookie?.value || null
}

/**
 * Read espn_s2 / SWID (httpOnly-capable) and relay them to PR 1's
 * X-Draft-Token endpoint. Never logs cookie values. Returns a small
 * {ok, error?} contract the content script maps to a user-facing hint.
 */
export async function connectEspnFromCookies(payload) {
  const draft_token = await getDraftToken()
  if (!draft_token) {
    return { ok: false, error: 'no_draft_token' }
  }

  const espn_s2 = await readEspnCookie('espn_s2')
  const swid = await readEspnCookie('SWID')
  if (!espn_s2 || !swid) {
    return { ok: false, error: 'no_espn_cookies' }
  }

  const resp = await fetch(`${getApiBase()}/leagues/connect/espn/extension`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Draft-Token': draft_token,
    },
    body: JSON.stringify({
      league_id: payload?.league_id,
      espn_s2,
      swid,
      ...(payload?.season ? { season: payload.season } : {}),
    }),
  })

  if (!resp.ok) {
    // Map PR 1's states to codes the popup surfaces — without echoing cookies.
    if (resp.status === 401) return { ok: false, error: 'invalid_draft_token' }
    if (resp.status === 422) return { ok: false, error: 'invalid_espn_cookies' }
    return { ok: false, error: `connect_failed_${resp.status}` }
  }
  const data = await resp.json().catch(() => ({}))
  return { ok: true, data }
}
