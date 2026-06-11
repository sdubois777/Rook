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
  const { data } = await api.get('/auth/yahoo/connect-url')
  return data.url
}
