import api from './client'

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
  // credentials must be included so the browser stores the HttpOnly nonce
  // cookie the backend sets here — it binds the OAuth callback to THIS browser.
  // Prod is same-origin (sent either way); this is for split-origin dev parity.
  const { data } = await api.get('/auth/yahoo/connect-url', { withCredentials: true })
  return data.url
}
