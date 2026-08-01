import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { pricingHookValue } from './pricingMock'

vi.mock('../api/waiver', () => ({
  fetchWaiverLeague: vi.fn(),
  fetchWaiverWire: vi.fn(),
  fetchWaiverRecommendations: vi.fn(),
}))
const h = vi.hoisted(() => ({ me: { tierLimits: null } }))
vi.mock('../hooks/useMe', () => ({ useMe: () => h.me }))
vi.mock('../hooks/usePricing', () => ({ usePricing: () => pricingHookValue() }))

import Waiver from '../pages/Waiver'
import {
  fetchWaiverLeague, fetchWaiverWire, fetchWaiverRecommendations,
} from '../api/waiver'

/**
 * The waiver page previously printed "$100 of $100 budget left" to every customer,
 * from a budget that had never been read from their league. These tests hold the
 * page to what the backend actually reports about each league.
 */

function leagueResponse(waiver = {}, team = {}) {
  return {
    season: 2026, week: 5, demo_mode: false, enforced: false,
    waiver_type: null, uses_bidding_budget: null, faab_budget: null,
    ...waiver,
    teams: [{
      team_id: 't1', team_name: 'My Team', is_me: true,
      faab_remaining: null, waiver_position: null, roster: [],
      ...team,
    }],
  }
}

function recsResponse(waiver, faab = {}) {
  return {
    season: 2026, week: 5, my_team_id: 't1', my_team_name: 'My Team',
    waiver, needs: [], silence: null, demo_mode: false, enforced: false,
    recommendations: [{
      add: {
        id: 'p9', name: 'Rico Dowdle', position: 'RB', nfl_team: 'CAR',
        forward_value: 60, forward_ppg: 12.4, value_trend: 'rising',
        confidence: 'full', buy_low: false, sell_high: false, injury_status: null,
      },
      drop: null, lineup_delta_ppw: 3.1, fills_need: false, need_positions: [],
      faab: {
        recommended: true, tier_label: 'week-winning starter', total_bid: 20,
        base_bid: 20, news_bump_bid: 0, pct_of_remaining: 0.2,
        why: 'week-winning starter', bid_applicable: true, ...faab,
      },
      news: null, matchup: null, why: 'improves your lineup',
    }],
  }
}

function renderWaiver() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><Waiver /></MemoryRouter>
    </QueryClientProvider>,
  )
}

async function runRecommendations() {
  fireEvent.click(await screen.findByRole('button', { name: /Find waiver targets/i }))
}

describe('Waiver page — the header never states a budget it did not read', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    h.me = { tierLimits: null }
    fetchWaiverWire.mockResolvedValue({ season: 2026, week: 5, players: [], demo_mode: false })
  })

  it('shows the real budget and balance when the league reported them', async () => {
    fetchWaiverLeague.mockResolvedValue(leagueResponse(
      { waiver_type: 'budget', uses_bidding_budget: true, faab_budget: 200 },
      { faab_remaining: 137 },
    ))
    renderWaiver()
    expect(await screen.findByText(/\$137 of \$200 budget left/)).toBeInTheDocument()
  })

  it('shows NO dollar figure for a league that does not bid', async () => {
    fetchWaiverLeague.mockResolvedValue(leagueResponse(
      { waiver_type: 'rolling priority', uses_bidding_budget: false, faab_budget: null },
      { waiver_position: 3 },
    ))
    renderWaiver()
    expect(await screen.findByText(/Rolling priority/)).toBeInTheDocument()
    expect(screen.getByText(/waiver priority #3/)).toBeInTheDocument()
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument()
    expect(screen.queryByText(/budget left/)).not.toBeInTheDocument()
  })

  it('claims nothing when the waiver system could not be read', async () => {
    // The response still carries no system — and crucially the page must not
    // invent one. Only the week and season may be stated.
    fetchWaiverLeague.mockResolvedValue(leagueResponse())
    renderWaiver()
    expect(await screen.findByText('Week 5, 2026')).toBeInTheDocument()
    expect(screen.queryByText(/budget/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument()
  })

  it('never fills an unknown team balance with the league budget', async () => {
    // The league bids and the budget is known, but this team's spend was not
    // reported. Showing the full budget as the balance is the original defect.
    fetchWaiverLeague.mockResolvedValue(leagueResponse(
      { waiver_type: 'budget', uses_bidding_budget: true, faab_budget: 200 },
      { faab_remaining: null },
    ))
    renderWaiver()
    expect(await screen.findByText(/\$200 budget/)).toBeInTheDocument()
    expect(screen.queryByText(/of \$200 budget left/)).not.toBeInTheDocument()
  })
})

describe('Waiver page — the recommendation panel labels assumed money', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    h.me = { tierLimits: null }
    fetchWaiverWire.mockResolvedValue({ season: 2026, week: 5, players: [], demo_mode: false })
  })

  it('says the BUDGET is the unknown when the league is known to bid', async () => {
    fetchWaiverLeague.mockResolvedValue(leagueResponse(
      { waiver_type: 'faab', uses_bidding_budget: true, faab_budget: null }))
    fetchWaiverRecommendations.mockResolvedValue(recsResponse({
      type: 'faab', uses_bidding_budget: true, budget: null, remaining: 100,
      waiver_position: null, budget_is_assumed: true, budget_basis: 'standard_budget',
    }))
    renderWaiver()
    await runRecommendations()

    expect(await screen.findByText(/could not read its budget/i)).toBeInTheDocument()
    expect(screen.getByText(/assume \$100/i)).toBeInTheDocument()
  })

  it('says the SYSTEM is the unknown when we never established that the league bids', async () => {
    // Every league row has these settings NULL until its next sync, so this is the
    // state the whole installed base is in on release day. Saying only "we could not
    // read your budget" here would assert that the league bids.
    fetchWaiverLeague.mockResolvedValue(leagueResponse())
    fetchWaiverRecommendations.mockResolvedValue(recsResponse({
      type: null, uses_bidding_budget: null, budget: null, remaining: 100,
      waiver_position: null, budget_is_assumed: true, budget_basis: 'unknown_system',
    }))
    renderWaiver()
    await runRecommendations()

    expect(await screen.findByText(/could not read how your league handles waivers/i))
      .toBeInTheDocument()
    expect(screen.getByText(/ignore the dollar amounts and use the ranking/i))
      .toBeInTheDocument()
    // And the header still states no budget of its own.
    expect(screen.queryByText(/budget left/)).not.toBeInTheDocument()
  })

  it('says the SPEND is the unknown when the budget was read but the spend was not', async () => {
    // Substituting the whole budget for an unreported spend assumes the customer has
    // not spent a cent. The page must not present that as their balance.
    fetchWaiverLeague.mockResolvedValue(leagueResponse(
      { waiver_type: 'budget', uses_bidding_budget: true, faab_budget: 200 }))
    fetchWaiverRecommendations.mockResolvedValue(recsResponse({
      type: 'budget', uses_bidding_budget: true, budget: 200, remaining: 200,
      waiver_position: null, budget_is_assumed: true, budget_basis: 'full_budget',
    }))
    renderWaiver()
    await runRecommendations()

    expect(await screen.findByText(/assume you still have all of it/i)).toBeInTheDocument()
    expect(screen.getByText(/\$200 budget, spend unknown/)).toBeInTheDocument()
    expect(screen.queryByText(/\$200 of \$200 budget left/)).not.toBeInTheDocument()
  })

  it('adds no caveat when the league\'s real budget was read', async () => {
    fetchWaiverLeague.mockResolvedValue(leagueResponse(
      { waiver_type: 'budget', uses_bidding_budget: true, faab_budget: 200 },
      { faab_remaining: 137 }))
    fetchWaiverRecommendations.mockResolvedValue(recsResponse({
      type: 'budget', uses_bidding_budget: true, budget: 200, remaining: 137,
      waiver_position: null, budget_is_assumed: false,
    }))
    renderWaiver()
    await runRecommendations()

    expect(await screen.findByText('$20')).toBeInTheDocument()
    expect(screen.queryByText(/assume/i)).not.toBeInTheDocument()
  })

  it('replaces the bid with the tier for a league that claims by priority', async () => {
    fetchWaiverLeague.mockResolvedValue(leagueResponse(
      { waiver_type: 'rolling priority', uses_bidding_budget: false }))
    fetchWaiverRecommendations.mockResolvedValue(recsResponse(
      {
        type: 'rolling priority', uses_bidding_budget: false, budget: null,
        remaining: null, waiver_position: 3, budget_is_assumed: false,
      },
      // What the backend sends for a non-bidding league: zeros meaning "not
      // applicable", flagged by bid_applicable.
      { total_bid: 0, base_bid: 0, pct_of_remaining: 0, bid_applicable: false },
    ))
    renderWaiver()
    await runRecommendations()

    // The tier survives; the money does not. Critically there must be no "$0"
    // and no "0%" — a confident-looking zero is still a fabricated figure.
    expect(await screen.findByText('week-winning starter')).toBeInTheDocument()
    expect(screen.getByText(/claim by waiver priority/)).toBeInTheDocument()
    expect(screen.queryByText('$0')).not.toBeInTheDocument()
    expect(screen.queryByText(/0% ·/)).not.toBeInTheDocument()
    expect(screen.getByText(/not by bidding/)).toBeInTheDocument()
  })

  it('only an explicit false suppresses the money on a card', async () => {
    // A serializer or proxy that stringifies booleans would otherwise print "$0" and
    // "0% ·" — the confident zero these cards exist to forbid.
    fetchWaiverLeague.mockResolvedValue(leagueResponse(
      { waiver_type: 'rolling priority', uses_bidding_budget: false }))
    fetchWaiverRecommendations.mockResolvedValue(recsResponse(
      {
        type: 'rolling priority', uses_bidding_budget: false, budget: null,
        remaining: null, waiver_position: 3, budget_is_assumed: false,
        budget_basis: 'no_bidding',
      },
      { total_bid: 0, base_bid: 0, pct_of_remaining: 0, bid_applicable: 'false' },
    ))
    renderWaiver()
    await runRecommendations()

    expect(await screen.findByText('week-winning starter')).toBeInTheDocument()
    expect(screen.queryByText('$0')).not.toBeInTheDocument()
    expect(screen.queryByText(/0% ·/)).not.toBeInTheDocument()
  })

  it('the header describes the same league the cards were priced against', async () => {
    // The league query is cached for five minutes. Without reconciliation the header
    // could state a budget directly above a note saying the league does not bid.
    fetchWaiverLeague.mockResolvedValue(leagueResponse(
      { waiver_type: 'budget', uses_bidding_budget: true, faab_budget: 200 },
      { faab_remaining: 137 },
    ))
    fetchWaiverRecommendations.mockResolvedValue(recsResponse(
      {
        type: 'rolling priority', uses_bidding_budget: false, budget: null,
        remaining: null, waiver_position: 3, budget_is_assumed: false,
        budget_basis: 'no_bidding',
      },
      { total_bid: 0, base_bid: 0, pct_of_remaining: 0, bid_applicable: false },
    ))
    renderWaiver()
    expect(await screen.findByText(/\$137 of \$200 budget left/)).toBeInTheDocument()

    await runRecommendations()
    expect(await screen.findByText(/not by bidding/)).toBeInTheDocument()
    // The stale budget clause is gone; header and panel now agree.
    expect(screen.queryByText(/\$137 of \$200 budget left/)).not.toBeInTheDocument()
    expect(screen.getByText(/waiver priority #3/)).toBeInTheDocument()
  })
})

describe('Waiver page — money is only ever shown for a team that is yours', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    h.me = { tierLimits: null }
    fetchWaiverWire.mockResolvedValue({ season: 2026, week: 5, players: [], demo_mode: false })
  })

  it('shows no balance when no team is identified as yours', async () => {
    // A stale team binding leaves every team unflagged. The acting-team fallback is
    // then another manager's roster, and printing their balance as the customer's
    // money is the same defect in a new place.
    fetchWaiverLeague.mockResolvedValue({
      season: 2026, week: 5, demo_mode: false, enforced: false,
      waiver_type: 'budget', uses_bidding_budget: true, faab_budget: 200,
      teams: [
        { team_id: 't1', team_name: 'A Stranger', is_me: false, faab_remaining: 137, waiver_position: null, roster: [] },
        { team_id: 't2', team_name: 'Also Not You', is_me: false, faab_remaining: 4, waiver_position: null, roster: [] },
      ],
    })
    renderWaiver()

    expect(await screen.findByText(/\$200 budget/)).toBeInTheDocument()
    expect(screen.queryByText(/\$137/)).not.toBeInTheDocument()
    expect(screen.queryByText(/budget left/)).not.toBeInTheDocument()
  })
})
