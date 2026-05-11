import { render, screen, fireEvent, act } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { useDraftStore } from '../stores/draft'

// Mock the API module
vi.mock('../api/draft', () => ({
  startDraft: vi.fn().mockResolvedValue({ status: 'started' }),
  getDraftState: vi.fn().mockResolvedValue({
    your_remaining_budget: 180,
    spendable_on_next_player: 165,
    roster_slots_remaining: 15,
    your_roster: [
      { player_id: '1', player_name: 'CMC', position: 'RB', price: 20 },
    ],
    positional_counts: { RB: 1 },
  }),
  getRecommendation: vi.fn().mockResolvedValue(null),
  placeBid: vi.fn().mockResolvedValue({ status: 'bid_placed' }),
  passNomination: vi.fn().mockResolvedValue({ status: 'passed' }),
  nominatePlayer: vi.fn().mockResolvedValue({ status: 'nominated' }),
  endDraft: vi.fn().mockResolvedValue({ status: 'ended' }),
  getAvailablePlayers: vi.fn().mockResolvedValue({
    tiers: {
      1: [
        { id: 'p1', name: 'Jonathan Taylor', position: 'RB', team_abbr: 'IND', ai_bid_ceiling: 55, market_value: 48, yahoo_player_id: 'y1' },
        { id: 'p2', name: 'Ja\'Marr Chase', position: 'WR', team_abbr: 'CIN', ai_bid_ceiling: 45, market_value: 50, yahoo_player_id: 'y2' },
      ],
      2: [
        { id: 'p3', name: 'Travis Kelce', position: 'TE', team_abbr: 'KC', ai_bid_ceiling: 30, market_value: 35, yahoo_player_id: 'y3' },
      ],
    },
  }),
  getOpponentBudgets: vi.fn().mockResolvedValue({ opponents: {} }),
}))

// Mock WebSocket
class MockWebSocket {
  constructor() {
    this.onopen = null
    this.onmessage = null
    this.onclose = null
    this.onerror = null
    setTimeout(() => this.onopen?.(), 0)
  }
  close() {}
  send() {}
}
vi.stubGlobal('WebSocket', MockWebSocket)

// Helper to reset store between tests
function resetStore() {
  useDraftStore.setState({
    phase: 'setup',
    wsStatus: 'disconnected',
    bridgeStatus: null,
    recommendation: null,
    currentBid: null,
    myBudget: 200,
    myRoster: [],
    rosterSlotsRemaining: 16,
    spendable: 200,
    positionalCounts: {},
    picks: [],
    opponentBudgets: {},
    comboAlerts: [],
    availablePlayers: [],
    availableFilter: { position: '', search: '' },
  })
}

// Lazy imports to avoid module-level issues
let DraftSetup, RecommendationPanel, MyRoster, AvailablePlayers, OpponentTracker

beforeEach(async () => {
  resetStore()
  // Dynamic imports
  DraftSetup = (await import('../components/draft/DraftSetup')).default
  RecommendationPanel = (await import('../components/draft/RecommendationPanel')).default
  MyRoster = (await import('../components/draft/MyRoster')).default
  AvailablePlayers = (await import('../components/draft/AvailablePlayers')).default
  OpponentTracker = (await import('../components/draft/OpponentTracker')).default
})

describe('DraftRoom', () => {
  it('renders setup screen initially', () => {
    render(
      <MemoryRouter>
        <DraftSetup />
      </MemoryRouter>
    )
    expect(screen.getByText('Start Draft Session')).toBeInTheDocument()
    expect(screen.getByText('Start Draft')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('e.g. team_1')).toBeInTheDocument()
  })

  it('start button calls API and transitions to live', async () => {
    const { startDraft } = await import('../api/draft')

    render(
      <MemoryRouter>
        <DraftSetup />
      </MemoryRouter>
    )

    const input = screen.getByPlaceholderText('e.g. team_1')
    fireEvent.change(input, { target: { value: 'team_5' } })

    const button = screen.getByText('Start Draft')
    await act(async () => {
      fireEvent.click(button)
    })

    // Store should have transitioned
    const state = useDraftStore.getState()
    expect(state.phase).toBe('live')
    expect(state.availablePlayers.length).toBe(3)
  })

  it('recommendation panel shows BUY with green styling', () => {
    useDraftStore.setState({
      phase: 'live',
      recommendation: {
        action: 'buy',
        bid_ceiling: 55,
        player_name: 'Jonathan Taylor',
        position: 'RB',
        confidence: 'high',
        reasoning: 'Elite RB1 at fair price',
        system_value: 50,
        market_value: 48,
        active_flags: [],
        opponent_alerts: [],
        block_value: 0,
        budget_allows_block: false,
        budget_summary: {
          your_remaining: 180,
          spendable_on_this_player: 165,
          roster_slots_remaining: 15,
        },
      },
    })

    const { container } = render(
      <MemoryRouter>
        <RecommendationPanel />
      </MemoryRouter>
    )

    expect(screen.getByText('BUY')).toBeInTheDocument()
    // Check green styling
    const actionEl = screen.getByText('BUY')
    expect(actionEl.className).toContain('text-emerald-400')
    expect(screen.getByText('Jonathan Taylor')).toBeInTheDocument()
    expect(screen.getByText('high')).toBeInTheDocument()
  })

  it('recommendation panel shows PASS with gray styling', () => {
    useDraftStore.setState({
      phase: 'live',
      recommendation: {
        action: 'pass',
        bid_ceiling: 1,
        player_name: 'Some Kicker',
        position: 'K',
        confidence: 'high',
        reasoning: 'Not worth a roster spot',
        system_value: 1,
        market_value: 1,
        active_flags: [],
        opponent_alerts: [],
        block_value: 0,
        budget_allows_block: false,
        budget_summary: {
          your_remaining: 200,
          spendable_on_this_player: 185,
          roster_slots_remaining: 16,
        },
      },
    })

    render(
      <MemoryRouter>
        <RecommendationPanel />
      </MemoryRouter>
    )

    expect(screen.getByText('PASS')).toBeInTheDocument()
    const actionEl = screen.getByText('PASS')
    expect(actionEl.className).toContain('text-slate-400')
  })

  it('bid button calls placeBid with ceiling amount', async () => {
    const { placeBid } = await import('../api/draft')

    useDraftStore.setState({
      phase: 'live',
      recommendation: {
        action: 'buy',
        bid_ceiling: 47,
        player_name: 'Test Player',
        position: 'WR',
        confidence: 'medium',
        reasoning: 'Good value',
        system_value: 45,
        market_value: 40,
        active_flags: [],
        opponent_alerts: [],
        block_value: 0,
        budget_allows_block: false,
        budget_summary: {
          your_remaining: 180,
          spendable_on_this_player: 165,
          roster_slots_remaining: 15,
        },
      },
    })

    render(
      <MemoryRouter>
        <RecommendationPanel />
      </MemoryRouter>
    )

    const bidButton = screen.getByText('Bid $47')
    await act(async () => {
      fireEvent.click(bidButton)
    })

    expect(placeBid).toHaveBeenCalledWith(47)
  })

  it('pass button shows confirmation before calling API', async () => {
    const { passNomination } = await import('../api/draft')
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)

    useDraftStore.setState({
      phase: 'live',
      recommendation: {
        action: 'buy',
        bid_ceiling: 30,
        player_name: 'Test Player',
        position: 'TE',
        confidence: 'low',
        reasoning: 'Decent option',
        system_value: 25,
        market_value: 28,
        active_flags: [],
        opponent_alerts: [],
        block_value: 0,
        budget_allows_block: false,
        budget_summary: {
          your_remaining: 200,
          spendable_on_this_player: 185,
          roster_slots_remaining: 16,
        },
      },
    })

    render(
      <MemoryRouter>
        <RecommendationPanel />
      </MemoryRouter>
    )

    const passButton = screen.getByText('Pass')
    await act(async () => {
      fireEvent.click(passButton)
    })

    // Confirm was called
    expect(confirmSpy).toHaveBeenCalled()
    // But API not called because we returned false
    expect(passNomination).not.toHaveBeenCalled()

    // Now confirm yes
    confirmSpy.mockReturnValue(true)
    await act(async () => {
      fireEvent.click(passButton)
    })

    expect(passNomination).toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('my roster shows budget bar and drafted players', () => {
    useDraftStore.setState({
      phase: 'live',
      myBudget: 142,
      spendable: 127,
      rosterSlotsRemaining: 11,
      myRoster: [
        { player_id: '1', player_name: 'CMC', position: 'RB', price: 58 },
      ],
    })

    render(
      <MemoryRouter>
        <MyRoster />
      </MemoryRouter>
    )

    expect(screen.getByText('$142')).toBeInTheDocument()
    expect(screen.getByText('$127')).toBeInTheDocument()
    expect(screen.getByText('CMC')).toBeInTheDocument()
    // $58 appears in both budget "Spent: $58" and roster row
    expect(screen.getAllByText('$58').length).toBeGreaterThanOrEqual(1)
  })

  it('available players filters by position', async () => {
    useDraftStore.setState({
      phase: 'live',
      availablePlayers: [
        { id: 'p1', name: 'Jonathan Taylor', position: 'RB', team_abbr: 'IND', ai_bid_ceiling: 55, market_value: 48 },
        { id: 'p2', name: 'Ja\'Marr Chase', position: 'WR', team_abbr: 'CIN', ai_bid_ceiling: 45, market_value: 50 },
        { id: 'p3', name: 'Travis Kelce', position: 'TE', team_abbr: 'KC', ai_bid_ceiling: 30, market_value: 35 },
      ],
    })

    render(
      <MemoryRouter>
        <AvailablePlayers />
      </MemoryRouter>
    )

    // All 3 visible initially
    expect(screen.getByText('Jonathan Taylor')).toBeInTheDocument()
    expect(screen.getByText("Ja'Marr Chase")).toBeInTheDocument()
    expect(screen.getByText('Travis Kelce')).toBeInTheDocument()

    // Filter to RB
    const select = screen.getByDisplayValue('All')
    await act(async () => {
      fireEvent.change(select, { target: { value: 'RB' } })
    })

    expect(screen.getByText('Jonathan Taylor')).toBeInTheDocument()
    expect(screen.queryByText("Ja'Marr Chase")).not.toBeInTheDocument()
    expect(screen.queryByText('Travis Kelce')).not.toBeInTheDocument()
  })

  it('draft pick WS message removes player from available', () => {
    useDraftStore.setState({
      phase: 'live',
      availablePlayers: [
        { id: 'p1', name: 'Jonathan Taylor', position: 'RB', team_abbr: 'IND', ai_bid_ceiling: 55, market_value: 48, yahoo_player_id: 'y1' },
        { id: 'p2', name: 'Ja\'Marr Chase', position: 'WR', team_abbr: 'CIN', ai_bid_ceiling: 45, market_value: 50, yahoo_player_id: 'y2' },
      ],
    })

    // Simulate a draft pick via store action
    act(() => {
      useDraftStore.getState().recordPick({
        player_id: 'y1',
        player_name: 'Jonathan Taylor',
        position: 'RB',
        team_id: 'team_3',
        final_price: 52,
      })
    })

    const state = useDraftStore.getState()
    expect(state.availablePlayers).toHaveLength(1)
    expect(state.availablePlayers[0].name).toBe("Ja'Marr Chase")
    expect(state.picks).toHaveLength(1)
  })

  it('opponent tracker shows combo alerts', () => {
    useDraftStore.setState({
      phase: 'live',
      comboAlerts: [
        { team_id: 'team_3', combos: ['Elite RB Stack'] },
        { team_id: 'team_5', combos: ['QB/WR Stack (KC)'] },
      ],
    })

    render(
      <MemoryRouter>
        <OpponentTracker />
      </MemoryRouter>
    )

    // Toggle the sidebar open
    const toggleButton = screen.getByRole('button')
    fireEvent.click(toggleButton)

    expect(screen.getByText('Recent Alerts')).toBeInTheDocument()
  })
})
