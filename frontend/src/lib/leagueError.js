/**
 * Map a trade/waiver/matchup league-load error to the right user-facing copy.
 *
 * The backend distinguishes three cases (recon: the old code mapped ANY 404 to a
 * "demo only" message — wrong once the pages are un-gated):
 *   - undrafted league  → 409, body { error: 'undrafted_league', message, signal }
 *   - no synced league  → 404 ("no synced league found — connect a league…")
 *   - anything else      → the caller's fallback
 */
export function leagueLoadMessage(error, fallback) {
  const data = error?.response?.data
  if (data?.error === 'undrafted_league') {
    return data.message || "This league hasn't drafted yet — come back after your draft."
  }
  if (error?.response?.status === 404) {
    return 'No synced league found — connect a league on the League page first.'
  }
  return fallback
}

/** True when the error is the "league hasn't drafted yet" empty state (not a real error). */
export function isUndrafted(error) {
  return error?.response?.data?.error === 'undrafted_league'
}

/**
 * True when auto-detect of the user's team failed (409 error=unbound_team). NOT a
 * dead end — the payload carries { league_id, teams } so the UI shows a team picker.
 */
export function isUnboundTeam(error) {
  return error?.response?.data?.error === 'unbound_team'
}

/** { leagueId, teams:[{team_id,name}] } from an unbound_team error, or null. */
export function unboundInfo(error) {
  const data = error?.response?.data
  if (data?.error !== 'unbound_team') return null
  return { leagueId: data.league_id, teams: data.teams || [] }
}
