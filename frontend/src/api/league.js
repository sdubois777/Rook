import api from './client'
import { YAHOO_ENABLED } from '../lib/constants'

export async function fetchLeagueTendencies(leagueId) {
  const { data } = await api.get('/league/tendencies', {
    params: { league_id: leagueId },
  })
  return data
}

export async function fetchUserLeagues() {
  const { data } = await api.get('/account/leagues')
  return data
}

export async function fetchYahooConnectUrl() {
  // Backstop below the UI gate: this is the ONLY network call that starts Yahoo OAuth,
  // so a future caller cannot silently reopen the flow past LeagueSetup's guards. Both
  // existing call sites already sit in try/catch and render a user-facing message, so
  // the throw degrades gracefully.
  if (!YAHOO_ENABLED) throw new Error('Yahoo connect is disabled')
  // credentials must be included so the browser stores the HttpOnly nonce
  // cookie the backend sets here — it binds the OAuth callback to THIS browser.
  // Prod is same-origin (sent either way); this is for split-origin dev parity.
  const { data } = await api.get('/auth/yahoo/connect-url', { withCredentials: true })
  return data.url
}
