import { describe, it, expect } from 'vitest'
import { leagueLoadMessage, isUndrafted, isUnboundTeam, unboundInfo } from '../lib/leagueError'

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

describe('unbound_team detection', () => {
  const err = {
    response: {
      status: 409,
      data: {
        error: 'unbound_team',
        league_id: 'lg-1',
        teams: [{ team_id: '2', name: 'Team Alpha' }, { team_id: '5', name: 'Bob' }],
      },
    },
  }

  it('isUnboundTeam is true only for unbound_team', () => {
    expect(isUnboundTeam(err)).toBe(true)
    expect(isUnboundTeam({ response: { status: 404 } })).toBe(false)
  })

  it('unboundInfo extracts leagueId + teams for the picker', () => {
    expect(unboundInfo(err)).toEqual({
      leagueId: 'lg-1',
      teams: [{ team_id: '2', name: 'Team Alpha' }, { team_id: '5', name: 'Bob' }],
    })
    expect(unboundInfo({ response: { status: 500 } })).toBeNull()
  })
})
