import { apiClient } from './client'

// Read-only demo league for the picker + team-switcher. DEMO-ONLY support surface
// (404s when WAIVER_DEMO_MODE is off). Teardown mirrors the trade demo — grep
// `fetchWaiverLeague`.
export async function fetchWaiverLeague() {
  const { data } = await apiClient.get('/waiver/league')
  return data
}

// FREE, un-metered browse list of the whole available wire pool — one row per free
// agent (name/position/nfl_team/forward_ppg), sorted by forward_ppg desc. Browsing
// never debits credits or checks the tier gate; only recommendations cost.
export async function fetchWaiverWire() {
  const { data } = await apiClient.get('/waiver/wire')
  return data
}

// Recommendations value the whole free-agent pool + run a news/depth-chart query,
// so allow a longer timeout than the 15s client default.
const REC_TIMEOUT_MS = 60000

// Rank the available pool for the acting team (add/drop by real-ppw lineup gain).
export async function fetchWaiverRecommendations({ myTeamId } = {}) {
  const { data } = await apiClient.post(
    '/waiver/recommendations',
    { my_team_id: myTeamId },
    { timeout: REC_TIMEOUT_MS },
  )
  return data
}
