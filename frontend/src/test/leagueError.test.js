import { describe, it, expect } from 'vitest'
import { leagueLoadMessage, isUndrafted } from '../lib/leagueError'

describe('leagueLoadMessage', () => {
  it('undrafted 409 → the backend message (not "demo only")', () => {
    const err = { response: { status: 409, data: { error: 'undrafted_league', message: "This league hasn't drafted yet — come back after your draft." } } }
    expect(leagueLoadMessage(err, 'fallback')).toMatch(/hasn't drafted/i)
    expect(isUndrafted(err)).toBe(true)
  })

  it('no-synced-league 404 → connect-a-league copy (NOT the demo message)', () => {
    const err = { response: { status: 404, data: { detail: 'no synced league found' } } }
    const msg = leagueLoadMessage(err, 'fallback')
    expect(msg).toMatch(/connect a league/i)
    expect(msg).not.toMatch(/demo/i)
    expect(isUndrafted(err)).toBe(false)
  })

  it('other errors → the caller fallback', () => {
    expect(leagueLoadMessage({ response: { status: 500 } }, 'Could not load the trade league.'))
      .toBe('Could not load the trade league.')
  })
})
