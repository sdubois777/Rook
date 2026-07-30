/**
 * The no-league format picker.
 *
 * Two invariants matter here. (1) It appears for a user with nothing synced — who
 * previously got an empty sidebar slot and no way to leave the accidental auction default.
 * (2) It does NOT appear once a league is synced, because LeagueContext ranks a synced
 * league above the manual choice: rendering the control there would offer authority it
 * does not have. LeagueSelector enforces the second by returning the toggle from its own
 * empty-list branch, so exactly one of the two controls can ever render.
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { LeagueProvider } from '../context/LeagueContext'

const h = vi.hoisted(() => ({ leagues: [] }))

vi.mock('@clerk/clerk-react', () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: true }),
}))
vi.mock('../api/league', () => ({
  fetchUserLeagues: () => Promise.resolve(h.leagues),
}))

import LeagueSelector from '../components/layout/LeagueSelector'

function renderSelector() {
  return render(
    <LeagueProvider>
      <LeagueSelector />
    </LeagueProvider>
  )
}

describe('NoLeagueFormatToggle', () => {
  beforeEach(() => {
    localStorage.clear()
    h.leagues = []
  })

  it('renders both selects when no league is synced', async () => {
    renderSelector()
    expect(await screen.findByLabelText('Draft type')).toBeInTheDocument()
    expect(screen.getByLabelText('Scoring format')).toBeInTheDocument()
    // The league dropdown must not be there — they are mutually exclusive.
    expect(screen.queryByLabelText('Select league')).not.toBeInTheDocument()
  })

  it('shows the resolved defaults: snake + PPR', async () => {
    renderSelector()
    expect(await screen.findByLabelText('Draft type')).toHaveValue('snake')
    expect(screen.getByLabelText('Scoring format')).toHaveValue('ppr')
  })

  it('does NOT render once a league is synced — the league outranks it', async () => {
    h.leagues = [{ id: 'lg1', league_name: 'Dynasty', draft_type: 'auction',
                   scoring: 'half_ppr', team_count: 12 }]
    renderSelector()
    expect(await screen.findByLabelText('Select league')).toBeInTheDocument()
    expect(screen.queryByLabelText('Draft type')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Scoring format')).not.toBeInTheDocument()
  })

  it('persists a change so it survives a reload', async () => {
    renderSelector()
    fireEvent.change(await screen.findByLabelText('Draft type'),
      { target: { value: 'auction' } })
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('leagueFormatOverride')))
        .toMatchObject({ draft_type: 'auction' })
    })
    expect(screen.getByLabelText('Draft type')).toHaveValue('auction')
  })

  it('the two selects are independent — changing scoring keeps the draft type', async () => {
    renderSelector()
    fireEvent.change(await screen.findByLabelText('Draft type'),
      { target: { value: 'auction' } })
    fireEvent.change(screen.getByLabelText('Scoring format'),
      { target: { value: 'standard' } })
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('leagueFormatOverride')))
        .toMatchObject({ draft_type: 'auction', scoring: 'standard' })
    })
  })

  it('offers every supported format', async () => {
    renderSelector()
    const scoring = await screen.findByLabelText('Scoring format')
    expect([...scoring.options].map((o) => o.value)).toEqual(['ppr', 'half_ppr', 'standard'])
    const draft = screen.getByLabelText('Draft type')
    expect([...draft.options].map((o) => o.value)).toEqual(['snake', 'auction'])
  })
})
