import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { pricingHookValue } from './pricingMock'

vi.mock('../api/trade', () => ({
  fetchTradeLeague: vi.fn(),
  analyzeTrade: vi.fn(),
  fetchTradeIdeas: vi.fn(),
}))
const h = vi.hoisted(() => ({ me: { tierLimits: null } }))
vi.mock('../hooks/useMe', () => ({ useMe: () => h.me }))
vi.mock('../hooks/usePricing', () => ({ usePricing: () => pricingHookValue() }))

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

/**
 * Gate-semantics flip: metered features are NEVER tier-locked in the UI.
 * Every tier sees the button; the credit cost renders from /billing/pricing
 * (never hardcoded); paid tiers run unlimited (the server no-ops the charge).
 * Insufficient credits surface as the 402 BillingNotice, not a lock.
 */
describe('Trade page credit labels (never tier-locked)', () => {
  beforeEach(() => {
    fetchTradeLeague.mockReset()
    h.me = { tierLimits: null }  // default: tier unknown → cost shown (safe)
  })

  it('demo: shows "no charge" label', async () => {
    fetchTradeLeague.mockResolvedValue(league(true))
    renderTrade()
    expect(await screen.findByText(/demo · no charge/i)).toBeInTheDocument()
    expect(screen.queryByText(/needs Standard/i)).not.toBeInTheDocument()
  })

  it('non-demo: analyze shows the fetched credit cost and is never locked', async () => {
    fetchTradeLeague.mockResolvedValue(league(false))
    renderTrade()
    expect(await screen.findByText(/Analyze my trade · 1 cr/i)).toBeInTheDocument()
    expect(screen.queryByText(/needs Standard/i)).not.toBeInTheDocument()
  })

  it('non-demo: the finder shows its fetched cost and is never Pro-locked', async () => {
    fetchTradeLeague.mockResolvedValue(league(false))
    renderTrade()
    await screen.findByText(/Analyze my trade · 1 cr/i)
    fireEvent.click(screen.getByRole('button', { name: /Trade ideas/i }))
    expect(await screen.findByText(/Give me trade ideas · 5 cr/i)).toBeInTheDocument()
    expect(screen.queryByText(/needs Pro/i)).not.toBeInTheDocument()
  })

  it('demo WITH enforcement: real cost labels, still no tier lock', async () => {
    fetchTradeLeague.mockResolvedValue(league(true, true))
    renderTrade()
    expect(await screen.findByText(/Analyze my trade · 1 cr/i)).toBeInTheDocument()
    expect(screen.queryByText(/no charge/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/needs Standard/i)).not.toBeInTheDocument()
  })

  it('paid (unlimited) tier: the action is shown with NO credit cost', async () => {
    h.me = { tierLimits: { unlimited_features: true } }
    fetchTradeLeague.mockResolvedValue(league(false))
    renderTrade()
    // The button still renders (paid users use the feature)…
    expect(await screen.findByRole('button', { name: /Analyze my trade/i })).toBeInTheDocument()
    // …but with no credit-cost note — unlimited pays nothing.
    expect(screen.queryByText(/1 cr/i)).not.toBeInTheDocument()
  })

  it('paid (unlimited) tier under demo enforcement: still no credit cost', async () => {
    h.me = { tierLimits: { unlimited_features: true } }
    fetchTradeLeague.mockResolvedValue(league(true, true))
    renderTrade()
    expect(await screen.findByRole('button', { name: /Analyze my trade/i })).toBeInTheDocument()
    expect(screen.queryByText(/1 cr/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/no charge/i)).not.toBeInTheDocument()
  })
})
