import { apiClient } from './client'

/**
 * Over-limit chooser API. When a downgrade drops the tier cap below the user's
 * active-league count, the account is "over limit" until they pick which active
 * leagues stay; the rest are parked (suspended) — never deleted.
 */

// { over_limit, active_count, max_leagues, candidates: [league...] }
export async function fetchLeagueLimitState() {
  const { data } = await apiClient.get('/account/leagues/limit-state')
  return data
}

// Keep these league ids active (<= cap); park the rest of the current-season set.
export async function resolveLeagueLimit(keepIds) {
  const { data } = await apiClient.post('/account/leagues/resolve-limit', {
    keep: keepIds,
  })
  return data
}
