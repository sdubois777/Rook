import api from './client'

export async function fetchTeams(params = {}) {
  const { data } = await api.get('/teams', { params })
  return data
}

export async function fetchTeam(abbr, scoringFormat = 'ppr') {
  const { data } = await api.get(`/teams/${abbr}`, { params: { scoring_format: scoringFormat } })
  return data
}
