import api from './client'

export async function fetchPlayers(params = {}) {
  const { data } = await api.get('/players', { params })
  return data
}

export async function searchPlayers(q) {
  const { data } = await api.get('/players/search', { params: { q } })
  return data
}

export async function fetchPlayerSummary() {
  const { data } = await api.get('/players/summary')
  return data
}

export async function fetchPlayer(id) {
  const { data } = await api.get(`/players/${id}`)
  return data
}
