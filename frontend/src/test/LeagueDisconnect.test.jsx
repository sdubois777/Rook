/**
 * A removed league must not outlive its own deletion.
 *
 * Two separate stores hold it: react-query's ['account'] cache (what the Account page
 * renders) and LeagueContext's localStorage-backed `selectedLeague` (what the sidebar and
 * the whole app's format resolution read). Deleting the league only ever updated the
 * first, so the sidebar kept offering it and every surface kept resolving draft type and
 * scoring from it.
 *
 * Worse, it could not self-heal: LeagueSelector's auto-select only ran when the fetch came
 * back NON-empty, so the "you have no leagues" case never cleared anything, and the
 * saved-league fallback re-rendered the dead league on every reload forever.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { LeagueProvider, useLeague } from '../context/LeagueContext'

const h = vi.hoisted(() => ({ leagues: [], reject: false }))

vi.mock('@clerk/clerk-react', () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: true }),
}))
vi.mock('../api/league', () => ({
  fetchUserLeagues: () =>
    h.reject ? Promise.reject(new Error('offline')) : Promise.resolve(h.leagues),
}))

import LeagueSelector from '../components/layout/LeagueSelector'

const DEAD = { id: 'gone', league_name: 'Old League', draft_type: 'auction',
               scoring: 'half_ppr', team_count: 12 }

function Probe() {
  const { isSnake, scoringFormat, selectedLeague } = useLeague()
  return (
    <div>
      <span data-testid="snake">{String(isSnake)}</span>
      <span data-testid="scoring">{scoringFormat}</span>
      <span data-testid="selected">{selectedLeague?.id || 'none'}</span>
    </div>
  )
}

function renderSidebar() {
  return render(
    <LeagueProvider>
      <LeagueSelector />
      <Probe />
    </LeagueProvider>
  )
}

describe('a deleted league is cleared from context', () => {
  beforeEach(() => {
    localStorage.clear()
    h.leagues = []
    h.reject = false
  })

  it('clears the saved league when the server reports none left', async () => {
    localStorage.setItem('selectedLeague', JSON.stringify(DEAD))
    renderSidebar()
    await waitFor(() => expect(screen.getByTestId('selected').textContent).toBe('none'))
    expect(localStorage.getItem('selectedLeague')).toBeNull()
  })

  it('reverts to the snake PPR default once it is gone', async () => {
    localStorage.setItem('selectedLeague', JSON.stringify(DEAD))
    renderSidebar()
    // Was resolving auction + half_ppr from the dead league.
    await waitFor(() => expect(screen.getByTestId('snake').textContent).toBe('true'))
    expect(screen.getByTestId('scoring').textContent).toBe('ppr')
  })

  it('swaps the sidebar dropdown for the format toggle', async () => {
    localStorage.setItem('selectedLeague', JSON.stringify(DEAD))
    renderSidebar()
    expect(await screen.findByLabelText('Draft type')).toBeInTheDocument()
    expect(screen.queryByLabelText('Select league')).not.toBeInTheDocument()
  })

  it('does NOT clear on a FAILED fetch — offline must not delete your league', async () => {
    // The saved-league fallback exists for exactly this case; clearing here would make a
    // dropped connection look like a deletion.
    localStorage.setItem('selectedLeague', JSON.stringify(DEAD))
    h.reject = true
    renderSidebar()
    expect(await screen.findByLabelText('Select league')).toBeInTheDocument()
    expect(screen.getByTestId('selected').textContent).toBe('gone')
    expect(screen.getByTestId('snake').textContent).toBe('false')
  })

  it('still auto-selects when leagues DO come back', async () => {
    const live = { id: 'lg1', league_name: 'Live', draft_type: 'snake',
                   scoring: 'ppr', team_count: 12 }
    h.leagues = [live]
    renderSidebar()
    await waitFor(() => expect(screen.getByTestId('selected').textContent).toBe('lg1'))
    expect(await screen.findByLabelText('Select league')).toBeInTheDocument()
  })

  it('replaces a stale saved league with a still-connected one', async () => {
    localStorage.setItem('selectedLeague', JSON.stringify(DEAD))
    h.leagues = [{ id: 'lg2', league_name: 'Other', draft_type: 'snake',
                   scoring: 'ppr', team_count: 10 }]
    renderSidebar()
    await waitFor(() => expect(screen.getByTestId('selected').textContent).toBe('lg2'))
  })
})
