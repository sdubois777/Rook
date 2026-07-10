import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LeagueContext } from '../context/LeagueContext'
import Dashboard from '../pages/Dashboard'
import { fetchUserLeagues } from '../api/league'

vi.mock('../api/players', () => ({
  fetchPlayers: vi.fn().mockResolvedValue({ players: [], total: 0 }),
  fetchPlayerSummary: vi.fn().mockResolvedValue({ position_counts: {} }),
}))
vi.mock('../api/news', () => ({ fetchNews: vi.fn().mockResolvedValue({ items: [] }) }))
vi.mock('../api/league', () => ({
  fetchUserLeagues: vi.fn(),
  fetchLeagueTendencies: vi.fn().mockResolvedValue({}),
}))

function renderDashboard(selectedLeague) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <LeagueContext.Provider
          value={{
            isSnake: false, isAuction: true, scoringFormat: 'ppr',
            selectedLeague, setSelectedLeague() {},
          }}
        >
          <Dashboard />
        </LeagueContext.Provider>
      </QueryClientProvider>
    </MemoryRouter>
  )
}

describe('Dashboard draft-date banner', () => {
  beforeEach(() => vi.clearAllMocks())

  it("shows the SELECTED league's real synced draft date — not a hardcoded one", async () => {
    fetchUserLeagues.mockResolvedValue([
      { id: 'L1', is_active: true, draft_type: 'auction', team_count: 12, draft_date: '2026-08-29T20:00:00+00:00' },
    ])
    renderDashboard({ id: 'L1', draft_type: 'auction' })
    expect(await screen.findByText('Draft: August 29, 2026')).toBeInTheDocument()
    // the old hardcoded date must NOT appear
    expect(screen.queryByText(/September 5, 2026/)).not.toBeInTheDocument()
  })

  it('renders the date in UTC so a near-midnight time does not roll to the wrong day', async () => {
    // 01:30 UTC on Sep 3 = Sep 2 in US timezones — must still read the synced day, Sep 3.
    fetchUserLeagues.mockResolvedValue([
      { id: 'L2', is_active: true, draft_type: 'auction', team_count: 12, draft_date: '2026-09-03T01:30:00+00:00' },
    ])
    renderDashboard({ id: 'L2', draft_type: 'auction' })
    expect(await screen.findByText('Draft: September 3, 2026')).toBeInTheDocument()
  })

  it('shows honest "not scheduled" copy (no fake date) for a null draft_date league', async () => {
    fetchUserLeagues.mockResolvedValue([
      { id: 'L3', is_active: true, draft_type: 'auction', team_count: 12, draft_date: null },
    ])
    renderDashboard({ id: 'L3', draft_type: 'auction' })
    expect(await screen.findByText('Draft date not scheduled')).toBeInTheDocument()
    expect(screen.queryByText(/^Draft: /)).not.toBeInTheDocument()   // no date line at all
  })

  it('is PER-LEAGUE: the banner tracks the selected league id', async () => {
    fetchUserLeagues.mockResolvedValue([
      { id: 'A', is_active: true, draft_type: 'auction', team_count: 12, draft_date: '2026-08-29T20:00:00+00:00' },
      { id: 'B', is_active: true, draft_type: 'auction', team_count: 12, draft_date: null },
    ])
    renderDashboard({ id: 'B', draft_type: 'auction' })   // select the null-draft league
    expect(await screen.findByText('Draft date not scheduled')).toBeInTheDocument()
    expect(screen.queryByText('Draft: August 29, 2026')).not.toBeInTheDocument()
  })
})
