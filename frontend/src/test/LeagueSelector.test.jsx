import { render, screen } from '@testing-library/react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { LeagueContext, LeagueProvider, useLeague } from '../context/LeagueContext'

// Clerk ready; league fetch returns nothing (simulates a slow/failed fetch on
// reload). The selector must still show the saved league.
vi.mock('@clerk/clerk-react', () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: true }),
}))
vi.mock('../api/league', () => ({
  fetchUserLeagues: vi.fn().mockResolvedValue([]),
}))

import LeagueSelector from '../components/layout/LeagueSelector'

beforeEach(() => {
  localStorage.clear()
})

describe('LeagueContext sync init', () => {
  it('initializes selectedLeague from localStorage synchronously', () => {
    localStorage.setItem(
      'selectedLeague',
      JSON.stringify({ id: 'x', draft_type: 'snake', scoring: 'ppr' })
    )
    let captured
    function Probe() {
      captured = useLeague()
      return null
    }
    // No await — the value must be present on the very first render.
    render(
      <LeagueProvider>
        <Probe />
      </LeagueProvider>
    )
    expect(captured.selectedLeague?.id).toBe('x')
    expect(captured.isSnake).toBe(true)
  })
})

describe('LeagueSelector reload persistence', () => {
  it('still shows the saved league when the leagues fetch returns nothing', async () => {
    const SAVED = {
      id: 'abc',
      league_name: "Stephen's Test",
      draft_type: 'snake',
      scoring: 'ppr',
      team_count: 12,
    }
    render(
      <LeagueContext.Provider
        value={{
          selectedLeague: SAVED,
          setSelectedLeague() {},
          isSnake: true,
          isAuction: false,
          scoringFormat: 'ppr',
        }}
      >
        <LeagueSelector />
      </LeagueContext.Provider>
    )

    const select = await screen.findByLabelText('Select league')
    expect(select.value).toBe('abc') // saved league still selected
    expect(screen.getByText("Stephen's Test")).toBeInTheDocument()
  })
})
