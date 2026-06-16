import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { useDraftStore } from '../stores/draft'
import { ACTION_STYLES } from '../components/draft/RecommendationPanel'
import { assignToSlot, POSITION_SLOTS } from '../components/draft/MyRoster'

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

// Mock the draftboard API (DraftRoom loads players from it on mount)
vi.mock('../api/draftboard', () => ({
  fetchDraftboard: vi.fn().mockResolvedValue({
    tiers: { 1: [{ id: 'd1', name: 'Mount Player', position: 'WR', ai_bid_ceiling: 20 }] },
  }),
}))

// Mock WebSocket — captures instances so tests can drive onmessage.
class MockWebSocket {
  static instances = []
  constructor() {
    this.onopen = null
    this.onmessage = null
    this.onclose = null
    this.onerror = null
    MockWebSocket.instances.push(this)
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
    currentNomination: null,
    teamsState: {},
    myBudget: 200,
    myRoster: [],
    myTeamName: null,
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
let DraftSetup, RecommendationPanel, MyRoster, AvailablePlayers, OpponentTracker, NominationPanel, DraftRoom

beforeEach(async () => {
  resetStore()
  MockWebSocket.instances = []
  // Dynamic imports
  DraftSetup = (await import('../components/draft/DraftSetup')).default
  RecommendationPanel = (await import('../components/draft/RecommendationPanel')).default
  MyRoster = (await import('../components/draft/MyRoster')).default
  AvailablePlayers = (await import('../components/draft/AvailablePlayers')).default
  OpponentTracker = (await import('../components/draft/OpponentTracker')).default
  NominationPanel = (await import('../components/draft/NominationPanel')).default
  DraftRoom = (await import('../pages/DraftRoom')).default
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
    expect(screen.getByPlaceholderText('Stephen — exactly as in the draft room')).toBeInTheDocument()
  })

  it('start button calls API and transitions to live', async () => {
    const { startDraft } = await import('../api/draft')

    render(
      <MemoryRouter>
        <DraftSetup />
      </MemoryRouter>
    )

    const input = screen.getByPlaceholderText('Stephen — exactly as in the draft room')
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

  it('setNomination shows the nominee and clears any stale recommendation', () => {
    useDraftStore.setState({
      phase: 'live',
      recommendation: { action: 'buy', player_name: 'Old Guy', bid_ceiling: 10 },
    })

    act(() => {
      useDraftStore.getState().setNomination({
        player_name: 'Sam LaPorta',
        pos_team: 'DET – TE',
        opening_bid: 4,
        clock: '0:19',
      })
    })

    const state = useDraftStore.getState()
    expect(state.recommendation).toBeNull()
    expect(state.currentNomination.playerName).toBe('Sam LaPorta')
    expect(state.currentNomination.secondsRemaining).toBe(19)
  })

  it('nomination panel turns the clock red under 10 seconds', () => {
    useDraftStore.setState({
      phase: 'live',
      currentNomination: {
        playerName: 'Sam LaPorta',
        posTeam: 'DET – TE',
        currentBid: 4,
        clock: '0:08',
        secondsRemaining: 8,
      },
    })

    render(
      <MemoryRouter>
        <NominationPanel />
      </MemoryRouter>
    )

    const clockEl = screen.getByText('0:08')
    expect(clockEl.className).toContain('text-red-500')
    expect(screen.getByText('Sam LaPorta')).toBeInTheDocument()
  })

  it('nomination panel shows team budgets with a threat indicator', () => {
    useDraftStore.setState({
      phase: 'live',
      currentNomination: {
        playerName: 'Sam LaPorta',
        posTeam: 'DET – TE',
        currentBid: 4,
        clock: '0:19',
        secondsRemaining: 19,
      },
      teamsState: {
        'Team 3': { budget: 149, slotsUsed: 0, totalSlots: 15 },
        Stephen: { budget: 9, slotsUsed: 6, totalSlots: 15 },
      },
    })

    render(
      <MemoryRouter>
        <NominationPanel />
      </MemoryRouter>
    )

    expect(screen.getByText('Team 3')).toBeInTheDocument()
    expect(screen.getByText('Stephen')).toBeInTheDocument()
    // Cash-heavy, few slots -> flagged; the small-budget team is not
    expect(screen.getByTitle('High budget, few slots filled')).toBeInTheDocument()
  })

  it('recordPick removes a relayed (name-only) pick from available', () => {
    useDraftStore.setState({
      phase: 'live',
      availablePlayers: [
        { id: 'p1', name: 'Jonathan Taylor', position: 'RB', yahoo_player_id: 'y1' },
        { id: 'p2', name: 'Travis Kelce', position: 'TE', yahoo_player_id: 'y3' },
      ],
    })

    // Extension relay payload — no player_id, only a name
    act(() => {
      useDraftStore.getState().recordPick({
        player_name: 'Jonathan Taylor',
        final_price: 52,
        winner: 'Stephen',
        teams_snapshot: { Stephen: { budget: 100, slotsUsed: 3, totalSlots: 15 } },
      })
    })

    const state = useDraftStore.getState()
    expect(state.availablePlayers).toHaveLength(1)
    expect(state.availablePlayers[0].name).toBe('Travis Kelce')
    expect(state.teamsState.Stephen.budget).toBe(100)
  })

  it('ACTION_STYLES has lowercase keys matching the engine actions', () => {
    expect(Object.keys(ACTION_STYLES).sort()).toEqual(
      ['bid_to', 'block', 'buy', 'pass']
    )
    // Every key must be lowercase (engine sends lowercase action strings)
    for (const key of Object.keys(ACTION_STYLES)) {
      expect(key).toBe(key.toLowerCase())
    }
  })

  it('DraftRoom polls the backend for the last recommendation on mount', async () => {
    const { getRecommendation } = await import('../api/draft')
    getRecommendation.mockResolvedValueOnce({
      type: 'recommendation',
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
    })

    useDraftStore.setState({ phase: 'live' })

    render(
      <MemoryRouter>
        <DraftRoom />
      </MemoryRouter>
    )

    // The mount poll should pull the engine's existing recommendation into
    // the store even though no WebSocket message ever arrived.
    await waitFor(() => {
      expect(getRecommendation).toHaveBeenCalled()
      expect(useDraftStore.getState().recommendation?.player_name).toBe(
        'Jonathan Taylor'
      )
    })
  })

  it('bid_update updates currentBid in the store', () => {
    useDraftStore.setState({
      phase: 'live',
      currentNomination: {
        playerName: 'Jonathan Taylor',
        posTeam: 'IND – RB',
        currentBid: 1,
        clock: '0:30',
        secondsRemaining: 30,
      },
    })

    act(() => {
      useDraftStore.getState().updateBid({
        player_name: 'Jonathan Taylor',
        current_bid: 45,
        clock: '0:15',
      })
    })

    const s = useDraftStore.getState()
    expect(s.currentNomination.currentBid).toBe(45)
    expect(s.currentNomination.secondsRemaining).toBe(15)
    expect(s.currentBid.current_bid).toBe(45)
  })

  it('draft_pick does not clear the available list, only removes the pick', () => {
    useDraftStore.setState({
      phase: 'live',
      availablePlayers: [
        { id: 'p1', name: 'Jonathan Taylor', position: 'RB', yahoo_player_id: 'y1' },
        { id: 'p2', name: 'Travis Kelce', position: 'TE', yahoo_player_id: 'y3' },
      ],
    })

    act(() => {
      // Case differs from the stored name — must still match and remove.
      useDraftStore.getState().recordPick({
        player_name: 'jonathan taylor',
        final_price: 52,
        winner: 'Team 3',
      })
    })

    const s = useDraftStore.getState()
    expect(s.availablePlayers).toHaveLength(1)
    expect(s.availablePlayers[0].name).toBe('Travis Kelce')
  })

  it('draft_pick adds to roster when the winner matches your team', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      myBudget: 200,
      rosterSlotsRemaining: 16,
      availablePlayers: [
        { id: 'p1', name: 'Jonathan Taylor', position: 'RB', yahoo_player_id: 'y1' },
      ],
    })

    act(() => {
      useDraftStore.getState().recordPick({
        player_name: 'Jonathan Taylor',
        final_price: 45,
        winner: 'Stephen',
      })
    })

    const s = useDraftStore.getState()
    expect(s.myRoster).toHaveLength(1)
    expect(s.myRoster[0].player_name).toBe('Jonathan Taylor')
    expect(s.myRoster[0].price).toBe(45)
    expect(s.myRoster[0].position).toBe('RB') // looked up from available
    expect(s.myBudget).toBe(155)
    expect(s.rosterSlotsRemaining).toBe(15)
  })

  it('draft_pick does NOT add to roster when another team wins', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      myRoster: [],
      availablePlayers: [
        { id: 'p1', name: 'Jonathan Taylor', position: 'RB', yahoo_player_id: 'y1' },
      ],
    })

    act(() => {
      useDraftStore.getState().recordPick({
        player_name: 'Jonathan Taylor',
        final_price: 45,
        winner: 'Team 7',
      })
    })

    expect(useDraftStore.getState().myRoster).toHaveLength(0)
  })

  it('a nomination clears the recommendation and polls for a fresh one', async () => {
    vi.useFakeTimers()
    try {
      const { getRecommendation } = await import('../api/draft')
      const rec = {
        type: 'recommendation',
        player_name: 'Sam LaPorta',
        action: 'buy',
        bid_ceiling: 18,
        confidence: 'high',
        reasoning: 'value',
        system_value: 15,
        market_value: 12,
        active_flags: [],
        opponent_alerts: [],
        block_value: 0,
        budget_allows_block: false,
      }
      getRecommendation.mockResolvedValue(rec)

      useDraftStore.setState({ phase: 'live' })
      render(
        <MemoryRouter>
          <DraftRoom />
        </MemoryRouter>
      )
      // Flush mount effects (on-mount poll + ws onopen timer)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1)
      })

      const ws = MockWebSocket.instances.at(-1)
      expect(ws).toBeTruthy()

      // Fire a nomination over the socket
      act(() => {
        ws.onmessage({
          data: JSON.stringify({
            type: 'nomination',
            payload: {
              player_name: 'Sam LaPorta',
              pos_team: 'DET – TE',
              opening_bid: 1,
              clock: '0:30',
            },
          }),
        })
      })

      // Recommendation cleared immediately; nominee shown
      expect(useDraftStore.getState().recommendation).toBeNull()
      expect(useDraftStore.getState().currentNomination.playerName).toBe('Sam LaPorta')

      // The 2.5s fallback poll fetches and applies the fresh recommendation
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2600)
      })

      expect(getRecommendation).toHaveBeenCalled()
      expect(useDraftStore.getState().recommendation?.player_name).toBe('Sam LaPorta')
    } finally {
      vi.useRealTimers()
    }
  })

  it('recordPick deduplicates the same player (second event ignored)', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      myBudget: 200,
      rosterSlotsRemaining: 16,
      availablePlayers: [
        { id: 'p1', name: 'Jonathan Taylor', position: 'RB', yahoo_player_id: 'y1' },
      ],
    })

    const pick = {
      player_name: 'Jonathan Taylor',
      final_price: 45,
      winner: 'Stephen',
    }

    act(() => {
      useDraftStore.getState().recordPick(pick)
      // Duplicate delivery (e.g. double-mounted socket) — must be ignored
      useDraftStore.getState().recordPick({ ...pick })
    })

    const s = useDraftStore.getState()
    expect(s.picks).toHaveLength(1)
    expect(s.myRoster).toHaveLength(1)
    expect(s.myBudget).toBe(155) // decremented once, not twice
  })

  it('recordPick does not add to roster when myTeamName is not set', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: null,
      myRoster: [],
      availablePlayers: [
        { id: 'p1', name: 'Jonathan Taylor', position: 'RB', yahoo_player_id: 'y1' },
      ],
    })

    act(() => {
      useDraftStore.getState().recordPick({
        player_name: 'Jonathan Taylor',
        final_price: 45,
        winner: 'Stephen',
      })
    })

    const s = useDraftStore.getState()
    expect(s.myRoster).toHaveLength(0)
    expect(s.availablePlayers).toHaveLength(0) // still removed from available
  })

  it('MyRoster shows the live budget from teamsState when available', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      myBudget: 200, // stale store value
      teamsState: {
        Stephen: { budget: 137, slotsUsed: 4, totalSlots: 15 },
        'Team 3': { budget: 90, slotsUsed: 6, totalSlots: 15 },
      },
    })

    render(
      <MemoryRouter>
        <MyRoster />
      </MemoryRouter>
    )

    // Live budget (137) wins over the stale store myBudget (200)
    expect(screen.getByText('$137')).toBeInTheDocument()
    expect(screen.getByText('$63')).toBeInTheDocument() // spent = 200 - 137
  })

  it('MyRoster falls back to myBudget when teamsState lacks your team', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      myBudget: 175,
      teamsState: { 'Team 3': { budget: 90, slotsUsed: 6, totalSlots: 15 } },
    })

    render(
      <MemoryRouter>
        <MyRoster />
      </MemoryRouter>
    )

    expect(screen.getByText('$175')).toBeInTheDocument()
  })

  it('startDraft does not overwrite availablePlayers with an empty result', async () => {
    const { getAvailablePlayers } = await import('../api/draft')
    getAvailablePlayers.mockResolvedValueOnce({ tiers: {} }) // empty draftboard

    useDraftStore.setState({
      availablePlayers: [
        { id: 'x', name: 'Existing Guy', position: 'RB' },
      ],
    })

    await act(async () => {
      await useDraftStore.getState().startDraft('Stephen')
    })

    const s = useDraftStore.getState()
    expect(s.phase).toBe('live')
    expect(s.availablePlayers).toHaveLength(1) // not wiped
    expect(s.availablePlayers[0].name).toBe('Existing Guy')
  })

  it('setAvailablePlayers ignores an empty array', () => {
    useDraftStore.setState({
      availablePlayers: [{ id: 'x', name: 'Keep Me', position: 'WR' }],
    })
    act(() => {
      useDraftStore.getState().setAvailablePlayers([])
    })
    expect(useDraftStore.getState().availablePlayers).toHaveLength(1)
  })

  it('DraftRoom loads available players on mount from the draftboard', async () => {
    // phase stays 'setup'; the mount effect still runs
    render(
      <MemoryRouter>
        <DraftRoom />
      </MemoryRouter>
    )
    await waitFor(() => {
      const list = useDraftStore.getState().availablePlayers
      expect(list.length).toBeGreaterThan(0)
      expect(list[0].name).toBe('Mount Player')
    })
  })

  it('assignToSlot puts a 3rd RB into FLEX when RB slots are full', () => {
    const grouped = {}
    assignToSlot({ player_name: 'RB1', position: 'RB' }, grouped, POSITION_SLOTS)
    assignToSlot({ player_name: 'RB2', position: 'RB' }, grouped, POSITION_SLOTS)
    assignToSlot({ player_name: 'RB3', position: 'RB' }, grouped, POSITION_SLOTS)
    expect(grouped.RB).toHaveLength(2)
    expect(grouped.FLEX).toHaveLength(1)
    expect(grouped.FLEX[0].player_name).toBe('RB3')
  })

  it('assignToSlot overflows to BN when RB and FLEX are full', () => {
    const grouped = {}
    // 2 RB (RB slots) + 1 RB (FLEX) + 1 more RB -> BN
    for (const name of ['RB1', 'RB2', 'RB3', 'RB4']) {
      assignToSlot({ player_name: name, position: 'RB' }, grouped, POSITION_SLOTS)
    }
    expect(grouped.RB).toHaveLength(2)
    expect(grouped.FLEX).toHaveLength(1)
    expect(grouped.BN).toHaveLength(1)
    expect(grouped.BN[0].player_name).toBe('RB4')
  })

  it('updateBid reconstructs a nomination when none is active', () => {
    useDraftStore.setState({ currentNomination: null, currentBid: null })
    act(() => {
      useDraftStore.getState().updateBid({
        player_name: 'Jonathan Taylor',
        current_bid: 30,
        clock: '0:12',
      })
    })
    const s = useDraftStore.getState()
    expect(s.currentNomination).not.toBeNull()
    expect(s.currentNomination.playerName).toBe('Jonathan Taylor')
    expect(s.currentNomination.currentBid).toBe(30)
    expect(s.currentNomination.secondsRemaining).toBe(12)
  })

  it('recordPick fills roster position from the available list when the pick has none', () => {
    useDraftStore.setState({
      myTeamName: 'Stephen',
      myRoster: [],
      availablePlayers: [
        { id: 'p1', name: 'Jonathan Taylor', position: 'RB', yahoo_player_id: 'y1' },
      ],
    })
    act(() => {
      useDraftStore.getState().recordPick({
        player_name: 'Jonathan Taylor', // no position on the pick
        final_price: 40,
        winner: 'Stephen',
      })
    })
    const roster = useDraftStore.getState().myRoster
    expect(roster).toHaveLength(1)
    expect(roster[0].position).toBe('RB')
  })

  it('a re-nomination of the same player does not reset the bid', async () => {
    useDraftStore.setState({
      phase: 'live',
      currentNomination: {
        playerName: 'Josh Allen',
        posTeam: 'BUF – QB',
        currentBid: 35,
        clock: '0:20',
        secondsRemaining: 20,
      },
      currentBid: { current_bid: 35, player_name: 'Josh Allen' },
    })

    render(
      <MemoryRouter>
        <DraftRoom />
      </MemoryRouter>
    )
    await act(async () => { await Promise.resolve() })
    const ws = MockWebSocket.instances.at(-1)

    act(() => {
      ws.onmessage({
        data: JSON.stringify({
          type: 'nomination',
          payload: { player_name: 'Josh Allen', opening_bid: 1, clock: '0:30' },
        }),
      })
    })

    const s = useDraftStore.getState()
    expect(s.currentNomination.currentBid).toBe(35) // NOT reset to 1
    expect(s.currentNomination.clock).toBe('0:30') // clock refreshed
  })

  it('a nomination for a new player clears the rec and resets the bid', async () => {
    useDraftStore.setState({
      phase: 'live',
      recommendation: { type: 'recommendation', player_name: 'Old', action: 'buy', bid_ceiling: 10 },
      currentNomination: { playerName: 'Old Guy', currentBid: 40 },
      currentBid: { current_bid: 40, player_name: 'Old Guy' },
    })

    render(
      <MemoryRouter>
        <DraftRoom />
      </MemoryRouter>
    )
    await act(async () => { await Promise.resolve() })
    const ws = MockWebSocket.instances.at(-1)

    act(() => {
      ws.onmessage({
        data: JSON.stringify({
          type: 'nomination',
          payload: { player_name: 'New Guy', opening_bid: 1, clock: '0:30' },
        }),
      })
    })

    const s = useDraftStore.getState()
    expect(s.currentNomination.playerName).toBe('New Guy')
    expect(s.currentNomination.currentBid).toBe(1)
    expect(s.recommendation).toBeNull()
  })

  it('recordPick dedup is time-bounded — a stale prior pick does not block', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: null,
      picks: [{ player_name: 'Josh Allen', timestamp: Date.now() - 3000 }], // 3s ago
      availablePlayers: [
        { id: 'p1', name: 'Josh Allen', position: 'QB', yahoo_player_id: 'y1' },
      ],
    })

    act(() => {
      useDraftStore.getState().recordPick({
        player_name: 'Josh Allen',
        final_price: 36,
        winner: 'Team 3',
      })
    })

    // Recorded again because the prior pick is older than 2s
    expect(useDraftStore.getState().picks).toHaveLength(2)
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
