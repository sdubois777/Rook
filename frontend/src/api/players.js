import api from './client'

export async function fetchPlayers(params = {}) {
  const { data } = await api.get('/players', { params })
  return data
}

export async function searchPlayers(q, scoringFormat = 'ppr') {
  const { data } = await api.get('/players/search', { params: { q, scoring_format: scoringFormat } })
  return data
}

export async function fetchPlayerSummary() {
  const { data } = await api.get('/players/summary')
  return data
}

export async function fetchPlayer(id, scoringFormat = 'ppr') {
  const { data } = await api.get(`/players/${id}`, { params: { scoring_format: scoringFormat } })
  return data
}
