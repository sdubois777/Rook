import api from './client'

export async function fetchTeams(params = {}) {
  const { data } = await api.get('/teams', { params })
  return data
}

export async function fetchTeam(abbr) {
  const { data } = await api.get(`/teams/${abbr}`)
  return data
}
