import api from './client'

export async function fetchPipelineStatus() {
  const { data } = await api.get('/admin/pipeline-status')
  return data
}

export async function triggerPipelineRun(agentName, teamAbbr = null) {
  const body = { agent_name: agentName }
  if (teamAbbr) body.team_abbr = teamAbbr
  const { data } = await api.post('/admin/pipeline/run', body)
  return data
}

export async function fetchCostReport(days = 30) {
  const { data } = await api.get('/admin/cost-report', { params: { days } })
  return data
}

export async function fetchDryRun(agentName) {
  const { data } = await api.post('/admin/pipeline/dry-run', { agent_name: agentName })
  return data
}

export async function fetchMarketValueStatus() {
  const { data } = await api.get('/pipeline/market-values/status')
  return data
}

export async function fetchBacktest(season = 2024) {
  const { data } = await api.get('/admin/backtest', { params: { season } })
  return data
}
