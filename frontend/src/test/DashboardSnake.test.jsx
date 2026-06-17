import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LeagueContext } from '../context/LeagueContext'
import Dashboard from '../pages/Dashboard'

vi.mock('../api/players', () => ({
  fetchPlayers: vi.fn((params) => {
    if (params?.snake_flag === 'VALUE')
      return Promise.resolve({ players: [{ id: 'v1', name: 'Value Guy', position: 'WR', adp_rank: 20, adp_fantasypros: 40, adp_diff: 20, snake_flag: 'VALUE' }], total: 1 })
    if (params?.snake_flag === 'SLEEPER')
      return Promise.resolve({ players: [{ id: 's1', name: 'Sleeper Guy', position: 'RB', adp_rank: 90, adp_fantasypros: 110, adp_diff: 20, snake_flag: 'SLEEPER' }], total: 1 })
    if (params?.snake_flag === 'REACH')
      return Promise.resolve({ players: [{ id: 'r1', name: 'Reach Guy', position: 'QB', adp_rank: 30, adp_fantasypros: 10, adp_diff: -20, snake_flag: 'REACH' }], total: 1 })
    return Promise.resolve({ players: [], total: 0 })
  }),
  fetchPlayerSummary: vi.fn().mockResolvedValue({ position_counts: {} }),
}))
vi.mock('../api/news', () => ({ fetchNews: vi.fn().mockResolvedValue({ items: [] }) }))
vi.mock('../api/league', () => ({
  fetchUserLeagues: vi.fn().mockResolvedValue([]),
  fetchLeagueTendencies: vi.fn().mockResolvedValue({}),
}))

function renderDashboard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <LeagueContext.Provider
          value={{
            isSnake: true,
            isAuction: false,
            scoringFormat: 'ppr',
            selectedLeague: { draft_type: 'snake', team_count: 12 },
            setSelectedLeague() {},
          }}
        >
          <Dashboard />
        </LeagueContext.Provider>
      </QueryClientProvider>
    </MemoryRouter>
  )
}

describe('Dashboard snake sections', () => {
  it('shows ADP-differential sections (value picks, reaches, sleepers) for snake', async () => {
    renderDashboard()
    expect(await screen.findByText('Top Value Picks')).toBeInTheDocument()
    expect(screen.getByText('Top Reaches to Avoid')).toBeInTheDocument()
    expect(screen.getByText('Sleepers')).toBeInTheDocument()
    // No auction "Top Value Gaps" card in snake mode.
    expect(screen.queryByText('Top Value Gaps')).not.toBeInTheDocument()
  })

  it('shows adp diff for snake players', async () => {
    renderDashboard()
    expect(await screen.findByText('Value Guy')).toBeInTheDocument()
    expect(screen.getByText('Sleeper Guy')).toBeInTheDocument()
    // adp_diff rendered (+20 for value/sleeper)
    expect(screen.getAllByText('+20').length).toBeGreaterThan(0)
  })
})
