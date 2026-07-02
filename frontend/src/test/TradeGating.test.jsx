import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/trade', () => ({
  fetchTradeLeague: vi.fn(),
  analyzeTrade: vi.fn(),
  fetchTradeIdeas: vi.fn(),
}))
const h = vi.hoisted(() => ({ limits: null }))
vi.mock('../hooks/useMe', () => ({ useMe: () => ({ tierLimits: h.limits }) }))

import Trade from '../pages/Trade'
import { fetchTradeLeague } from '../api/trade'

function league(demo, enforced = false) {
  return {
    week: 14, season: 2025, demo_mode: demo, enforced,
    teams: [
      { team_id: 'a', team_name: 'A', is_me: true, roster: [
        { id: 'p1', name: 'P1', position: 'RB', forward_value: 20, value_trend: 'stable', confidence: 'full' },
      ] },
      { team_id: 'b', team_name: 'B', is_me: false, roster: [
        { id: 'p2', name: 'P2', position: 'WR', forward_value: 30, value_trend: 'stable', confidence: 'full' },
      ] },
    ],
  }
}

function renderTrade() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><Trade /></MemoryRouter>
    </QueryClientProvider>
  )
}

describe('Trade page gating + credit labels', () => {
  beforeEach(() => {
    fetchTradeLeague.mockReset()
    h.limits = null
  })

  it('demo: shows "no charge" label and never locks (bypasses the gate)', async () => {
    h.limits = { trade_analyzer: false, trade_finder: false }
    fetchTradeLeague.mockResolvedValue(league(true))
    renderTrade()
    expect(await screen.findByText(/demo · no charge/i)).toBeInTheDocument()
    expect(screen.queryByText(/needs Standard/i)).not.toBeInTheDocument()
  })

  it('non-demo intro: analyze is locked behind Standard', async () => {
    h.limits = { trade_analyzer: false, trade_finder: false }
    fetchTradeLeague.mockResolvedValue(league(false))
    renderTrade()
    expect(await screen.findByText(/Trade analyzer needs Standard/i)).toBeInTheDocument()
  })

  it('non-demo standard: analyze shows 10 cr; the finder tab is locked behind Pro', async () => {
    h.limits = { trade_analyzer: true, trade_finder: false }
    fetchTradeLeague.mockResolvedValue(league(false))
    renderTrade()
    expect(await screen.findByText(/Analyze my trade · 10 cr/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Trade ideas/i }))
    expect(await screen.findByText(/Trade finder needs Pro/i)).toBeInTheDocument()
  })

  it('non-demo pro: the finder shows its 20 cr cost', async () => {
    h.limits = { trade_analyzer: true, trade_finder: true }
    fetchTradeLeague.mockResolvedValue(league(false))
    renderTrade()
    await screen.findByText(/Analyze my trade · 10 cr/i)
    fireEvent.click(screen.getByRole('button', { name: /Trade ideas/i }))
    expect(await screen.findByText(/Give me trade ideas · 20 cr/i)).toBeInTheDocument()
  })

  it('demo WITH enforcement: charges shown + gate applies (intro locked)', async () => {
    h.limits = { trade_analyzer: false, trade_finder: false }
    fetchTradeLeague.mockResolvedValue(league(true, true)) // demo + enforced
    renderTrade()
    // Not "no charge" anymore, and the gate locks like real.
    expect(await screen.findByText(/Trade analyzer needs Standard/i)).toBeInTheDocument()
    expect(screen.queryByText(/no charge/i)).not.toBeInTheDocument()
  })

  it('demo WITH enforcement: standard sees the 10 cr cost (not "no charge")', async () => {
    h.limits = { trade_analyzer: true, trade_finder: false }
    fetchTradeLeague.mockResolvedValue(league(true, true))
    renderTrade()
    expect(await screen.findByText(/Analyze my trade · 10 cr/i)).toBeInTheDocument()
  })
})
