import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/matchup', () => ({ fetchMatchupLeague: vi.fn() }))

import Matchup from '../pages/Matchup'
import { fetchMatchupLeague } from '../api/matchup'

/**
 * The matchup page must never present an invented opponent as a real one.
 *
 * It used to pair teams with a round-robin generator for every league on every week,
 * with nothing saying so. Measured against real leagues, that named the correct
 * opponent for about one team-week in ten, and the projected margin, win band,
 * positional grid and the trade opening it hands to the paid analyzer were all
 * computed against a team the customer was not playing.
 */
function response({ scheduleSource, scout = null }) {
  return {
    season: 2026, week: 5, my_team_id: 'me', my_team_name: 'My Team',
    demo_mode: false, enforced: false,
    schedule_source: scheduleSource,
    matchups: [],
    teams: [
      { team_id: 'me', team_name: 'My Team', is_me: true, strength: 100, ppw: 110 },
      { team_id: 'b', team_name: 'Rivals B', is_me: false, strength: 90, ppw: 100 },
    ],
    scout,
  }
}

function renderMatchup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><Matchup /></MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Matchup page — no invented opponent', () => {
  beforeEach(() => vi.clearAllMocks())

  it('says the schedule could not be read, and does not blame a bye', async () => {
    fetchMatchupLeague.mockResolvedValue(response({ scheduleSource: 'unavailable' }))
    renderMatchup()

    expect(await screen.findByText(/could not read your league's schedule/i)).toBeInTheDocument()
    expect(screen.getByText(/rather than guess who you are playing/i)).toBeInTheDocument()
    // "bye" would be a specific, different, and wrong explanation.
    expect(screen.queryByText(/bye/i)).not.toBeInTheDocument()
  })

  it('still shows the strength ladder, which does not depend on an opponent', async () => {
    fetchMatchupLeague.mockResolvedValue(response({ scheduleSource: 'unavailable' }))
    renderMatchup()

    expect(await screen.findByText('Rivals B')).toBeInTheDocument()
  })

  it('calls it a bye only when the real schedule genuinely has no game', async () => {
    fetchMatchupLeague.mockResolvedValue(response({ scheduleSource: 'league' }))
    renderMatchup()

    expect(await screen.findByText(/has no game this week/i)).toBeInTheDocument()
    expect(screen.queryByText(/could not read/i)).not.toBeInTheDocument()
  })
})
