import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/leagues', () => ({
  fetchLeagueLimitState: vi.fn(),
  resolveLeagueLimit: vi.fn(),
}))

import LeagueChooser from '../components/billing/LeagueChooser'
import { fetchLeagueLimitState, resolveLeagueLimit } from '../api/leagues'

function state(over) {
  const mk = (id) => ({
    id, league_name: id.toUpperCase(), platform: 'yahoo',
    scoring: 'ppr', season_year: 2026, suspended: false,
  })
  return { over_limit: over, active_count: 3, max_leagues: 2, candidates: [mk('a'), mk('b'), mk('c')] }
}

function renderChooser() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <LeagueChooser />
    </QueryClientProvider>
  )
}

describe('LeagueChooser', () => {
  beforeEach(() => {
    fetchLeagueLimitState.mockReset()
    resolveLeagueLimit.mockReset()
  })

  it('renders nothing when not over limit', async () => {
    fetchLeagueLimitState.mockResolvedValue(state(false))
    renderChooser()
    // give the query a tick; still nothing
    await waitFor(() => expect(fetchLeagueLimitState).toHaveBeenCalled())
    expect(screen.queryByText('Choose your active leagues')).not.toBeInTheDocument()
  })

  it('over limit: shows the chooser, enforces the cap, and resolves the keep set', async () => {
    fetchLeagueLimitState.mockResolvedValue(state(true))
    resolveLeagueLimit.mockResolvedValue({})
    renderChooser()

    await screen.findByText('Choose your active leagues')

    const boxes = screen.getAllByRole('checkbox')
    expect(boxes).toHaveLength(3)
    // default keeps the first `cap` (2) active; the third is at-cap and disabled
    expect(boxes[0]).toBeChecked()
    expect(boxes[1]).toBeChecked()
    expect(boxes[2]).not.toBeChecked()
    expect(boxes[2]).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: /Keep 2 of 2 active/i }))
    await waitFor(() => expect(resolveLeagueLimit).toHaveBeenCalledWith(['a', 'b']))
  })

  it('unchecking one frees a slot for another', async () => {
    fetchLeagueLimitState.mockResolvedValue(state(true))
    renderChooser()
    await screen.findByText('Choose your active leagues')
    const boxes = screen.getAllByRole('checkbox')
    fireEvent.click(boxes[0])          // uncheck a
    expect(boxes[2]).not.toBeDisabled() // c now selectable
    fireEvent.click(boxes[2])          // keep b + c
    fireEvent.click(screen.getByRole('button', { name: /Keep 2 of 2 active/i }))
    await waitFor(() => expect(resolveLeagueLimit).toHaveBeenCalledWith(['b', 'c']))
  })
})
