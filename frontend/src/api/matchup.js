import { apiClient } from './client'

// Read-only H2H scouting for the acting team (this week's opponent, positional
// grid, roster-strength ladder, needs/surplus leverage). ZERO-metered — every
// number is a pure primitive, the SAME evaluate_league basis as /trade/league.
// DEMO-ONLY support surface (404s when TRADE_DEMO_MODE is off); teardown mirrors
// the trade demo — grep `fetchMatchupLeague`.
export async function fetchMatchupLeague({ myTeamId } = {}) {
  const { data } = await apiClient.get('/matchup/league', {
    params: myTeamId ? { my_team_id: myTeamId } : undefined,
  })
  return data
}
