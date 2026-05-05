import api from './client'

export async function fetchWatchlist() {
  const { data } = await api.get('/preferences/watchlist')
  return data
}

export async function addToWatchlist(playerId) {
  const { data } = await api.post('/preferences/watchlist', { player_id: playerId })
  return data
}

export async function removeFromWatchlist(playerId) {
  await api.delete(`/preferences/watchlist/${playerId}`)
}

export async function fetchStrategy() {
  const { data } = await api.get('/preferences/strategy')
  return data
}

export async function setStrategy(strategy) {
  const { data } = await api.put('/preferences/strategy', { strategy })
  return data
}
