import { apiClient } from './client'

// Read-only demo league for the picker + team-switcher. DEMO-ONLY support
// surface (404s when TRADE_DEMO_MODE is off). Teardown: slice 6 — grep
// `fetchTradeLeague`.
export async function fetchTradeLeague() {
  const { data } = await apiClient.get('/trade/league')
  return data
}

// These endpoints run live Sonnet calls (analyze = 1; ideas = candidate gen +
// one rationale per surfaced proposal), so they need a longer timeout than the
// 15s client default.
const LLM_TIMEOUT_MS = 60000

// Evaluate a trade the user built. Returns the deterministic verdict payload.
export async function analyzeTrade({ myTeamId, give, get }) {
  const { data } = await apiClient.post(
    '/trade/analyze',
    { my_team_id: myTeamId, give, get },
    { timeout: LLM_TIMEOUT_MS },
  )
  return data
}

// Ask the system for trade ideas (0-5). Empty list + "no clear trade right now"
// is a valid result.
export async function fetchTradeIdeas({ myTeamId } = {}) {
  const { data } = await apiClient.post(
    '/trade/ideas',
    { my_team_id: myTeamId },
    { timeout: LLM_TIMEOUT_MS },
  )
  return data
}
