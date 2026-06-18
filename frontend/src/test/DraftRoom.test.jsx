import { render, screen, fireEvent, act, waitFor, within } from '@testing-library/react'
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
    isYourTurn: false,
    currentPick: null,
    currentRound: null,
    picksUntilYourTurn: null,
    myBudget: 200,
    myRoster: [],
    myTeamName: null,
    rosterSlotsRemaining: 16,
    spendable: 200,
    positionalCounts: {},
    picks: [],
    opponentBudgets: {},
    comboAlerts: [],
    teamPicks: {},
    teamThreatScores: {},
    selectedTeam: null,
    availablePlayers: [],
    availableFilter: { position: '', search: '' },
  })
}

// Lazy imports to avoid module-level issues
let DraftSetup, RecommendationPanel, MyRoster, AvailablePlayers, OpponentTracker, NominationPanel, TeamRosterPanel, DraftRoom

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
  TeamRosterPanel = (await import('../components/draft/TeamRosterPanel')).default
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

  // NOTE: the redesigned RecommendationPanel is read-only — the bid and pass
  // buttons were removed (you execute in Yahoo, DraftMind only advises), so the
  // former "bid button calls placeBid" / "pass button confirms" tests are gone.

  it('recommendation panel renders no bid or pass buttons', () => {
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

    // The recommendation content still renders...
    expect(screen.getByText('BUY')).toBeInTheDocument()
    // ...but there are no action buttons.
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByText('Pass')).not.toBeInTheDocument()
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

  it('nomination panel shows a value indicator from bid vs system value', () => {
    useDraftStore.setState({
      phase: 'live',
      currentNomination: {
        playerName: 'Sam LaPorta',
        posTeam: 'DET – TE',
        currentBid: 4,
        clock: '0:19',
        secondsRemaining: 19,
      },
      // bid 4 sits well under system 25 -> Undervalued
      recommendation: { player_name: 'Sam LaPorta', system_value: 25, market_value: 28 },
    })

    render(
      <MemoryRouter>
        <NominationPanel />
      </MemoryRouter>
    )

    expect(screen.getByText('Undervalued')).toBeInTheDocument()
    // Team budgets no longer live in the nomination panel — they moved to the
    // Team Rosters panel on the right.
    expect(screen.queryByText('Team Budgets')).not.toBeInTheDocument()
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

  it('setNomination preserves a higher existing bid for the same player', () => {
    useDraftStore.setState({
      currentNomination: null,
      currentBid: { current_bid: 43, player_name: 'Josh Allen' },
    })
    act(() => {
      useDraftStore.getState().setNomination({
        player_name: 'Josh Allen',
        pos_team: 'BUF – QB',
        opening_bid: 1,
        clock: '0:25',
      })
    })
    const s = useDraftStore.getState()
    expect(s.currentNomination.currentBid).toBe(43) // not clobbered by opening 1
    expect(s.currentBid.current_bid).toBe(43)
  })

  it('setNomination resets the bid for a different player', () => {
    useDraftStore.setState({
      currentBid: { current_bid: 43, player_name: 'Josh Allen' },
    })
    act(() => {
      useDraftStore.getState().setNomination({
        player_name: 'Bijan Robinson',
        opening_bid: 1,
        clock: '0:30',
      })
    })
    const s = useDraftStore.getState()
    expect(s.currentNomination.playerName).toBe('Bijan Robinson')
    expect(s.currentNomination.currentBid).toBe(1)
  })

  it('setNomination uses opening_bid when it exceeds the prior bid', () => {
    useDraftStore.setState({
      currentBid: { current_bid: 1, player_name: 'Josh Allen' },
    })
    act(() => {
      useDraftStore.getState().setNomination({
        player_name: 'Josh Allen',
        opening_bid: 5,
        clock: '0:30',
      })
    })
    expect(useDraftStore.getState().currentNomination.currentBid).toBe(5)
  })

  it('a nomination does NOT refetch the draftboard', async () => {
    const { getAvailablePlayers } = await import('../api/draft')
    const { fetchDraftboard } = await import('../api/draftboard')
    useDraftStore.setState({ phase: 'live', currentNomination: null })

    render(
      <MemoryRouter>
        <DraftRoom />
      </MemoryRouter>
    )
    // Let the on-mount load run, then clear the spies so we only observe
    // what the nomination itself triggers.
    await act(async () => { await Promise.resolve() })
    getAvailablePlayers.mockClear()
    fetchDraftboard.mockClear()
    const ws = MockWebSocket.instances.at(-1)

    act(() => {
      ws.onmessage({
        data: JSON.stringify({
          type: 'nomination',
          payload: { player_name: 'Sam LaPorta', opening_bid: 1, clock: '0:30' },
        }),
      })
    })
    await act(async () => { await Promise.resolve() })

    expect(getAvailablePlayers).not.toHaveBeenCalled()
    expect(fetchDraftboard).not.toHaveBeenCalled()
  })

  it('available count is unchanged by a nomination and drops by one on a pick', () => {
    useDraftStore.setState({
      phase: 'live',
      currentNomination: null,
      availablePlayers: [
        { id: 'p1', name: 'Jonathan Taylor', position: 'RB', yahoo_player_id: 'y1' },
        { id: 'p2', name: "Ja'Marr Chase", position: 'WR', yahoo_player_id: 'y2' },
        { id: 'p3', name: 'Travis Kelce', position: 'TE', yahoo_player_id: 'y3' },
      ],
    })

    act(() => {
      useDraftStore.getState().setNomination({
        player_name: 'Jonathan Taylor', opening_bid: 1, clock: '0:30',
      })
    })
    expect(useDraftStore.getState().availablePlayers).toHaveLength(3) // nomination: no change

    act(() => {
      useDraftStore.getState().recordPick({
        player_name: 'Jonathan Taylor', final_price: 40, winner: 'Team 3',
      })
    })
    expect(useDraftStore.getState().availablePlayers).toHaveLength(2) // pick: -1
  })

  it('a sold pick (relay shape: name only) removes ONLY that player, not the list', () => {
    // Real draftboard players have `id` + `name` but NO yahoo_player_id, and the
    // relayed draft_pick has NO player_id. The filter must not wipe the list.
    useDraftStore.setState({
      phase: 'live',
      myTeamName: null,
      picks: [],
      availablePlayers: [
        { id: 'a1b2', name: 'Josh Allen', position: 'QB' },
        { id: 'c3d4', name: 'Bijan Robinson', position: 'RB' },
        { id: 'e5f6', name: 'CeeDee Lamb', position: 'WR' },
      ],
    })

    act(() => {
      useDraftStore.getState().recordPick({
        player_name: 'Josh Allen', // no player_id, as the relay sends
        final_price: 40,
        winner: 'Team 3',
      })
    })

    const remaining = useDraftStore.getState().availablePlayers.map((p) => p.name)
    expect(remaining).toHaveLength(2) // NOT wiped to 0
    expect(remaining).toEqual(['Bijan Robinson', 'CeeDee Lamb'])
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

  // --- Team Rosters panel (redesign) ---

  it('teamPicks accumulates picks by winner', () => {
    useDraftStore.setState({
      phase: 'live',
      availablePlayers: [
        { id: 'p1', name: 'CMC', position: 'RB', ai_bid_ceiling: 72 },
        { id: 'p2', name: 'Puka Nacua', position: 'WR', ai_bid_ceiling: 60 },
        { id: 'p3', name: 'Sauce Gardner', position: 'CB', ai_bid_ceiling: 5 },
      ],
    })

    act(() => {
      useDraftStore.getState().recordPick({ player_name: 'CMC', final_price: 50, winner: 'Team 3' })
      useDraftStore.getState().recordPick({ player_name: 'Puka Nacua', final_price: 40, winner: 'Team 3' })
      useDraftStore.getState().recordPick({ player_name: 'Sauce Gardner', final_price: 3, winner: 'Team 7' })
    })

    const { teamPicks } = useDraftStore.getState()
    expect(teamPicks['Team 3']).toHaveLength(2)
    expect(teamPicks['Team 3'].map((p) => p.player_name)).toEqual(['CMC', 'Puka Nacua'])
    expect(teamPicks['Team 7']).toHaveLength(1)
  })

  it('threatScore sums ai_bid_ceiling', () => {
    useDraftStore.setState({
      phase: 'live',
      availablePlayers: [
        { id: 'p1', name: 'CMC', position: 'RB', ai_bid_ceiling: 72 },
        { id: 'p2', name: 'Puka Nacua', position: 'WR', ai_bid_ceiling: 60 },
      ],
    })

    act(() => {
      useDraftStore.getState().recordPick({ player_name: 'CMC', final_price: 50, winner: 'Team 3' })
      useDraftStore.getState().recordPick({ player_name: 'Puka Nacua', final_price: 40, winner: 'Team 3' })
    })

    // Ceiling-based, not price-based: 72 + 60 = 132 (prices were 50 + 40 = 90).
    expect(useDraftStore.getState().teamThreatScores['Team 3']).toBe(132)
  })

  it('teams sorted by threat score in dropdown', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: null,
      teamsState: {
        'Team A': { budget: 50 },
        'Team B': { budget: 50 },
        'Team C': { budget: 50 },
      },
      teamThreatScores: { 'Team A': 50, 'Team B': 200, 'Team C': 120 },
    })

    render(
      <MemoryRouter>
        <TeamRosterPanel />
      </MemoryRouter>
    )

    const options = within(screen.getByLabelText('Select team')).getAllByRole('option')
    const order = options.map((o) => o.textContent)
    expect(order[0]).toContain('Team B') // highest threat first
    expect(order[1]).toContain('Team C')
    expect(order[2]).toContain('Team A')
  })

  it('my team always first in dropdown', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      teamsState: { Stephen: { budget: 50 }, 'Team B': { budget: 50 } },
      teamThreatScores: { 'Team B': 200, Stephen: 10 }, // Team B more dangerous
    })

    render(
      <MemoryRouter>
        <TeamRosterPanel />
      </MemoryRouter>
    )

    const options = within(screen.getByLabelText('Select team')).getAllByRole('option')
    // Despite Team B's higher threat, my team is pinned first.
    expect(options[0].textContent).toContain('Stephen')
    expect(options[0].textContent).toContain('(you)')
  })

  it('opponent roster shows name + price', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      selectedTeam: 'Team 3',
      teamsState: { 'Team 3': { budget: 100, slotsUsed: 1, totalSlots: 16 } },
      teamPicks: {
        'Team 3': [{ player_name: 'CMC', price: 50, position: 'RB', ceiling: 72 }],
      },
    })

    render(
      <MemoryRouter>
        <TeamRosterPanel />
      </MemoryRouter>
    )

    expect(screen.getByText('CMC')).toBeInTheDocument()
    expect(screen.getByText('$50')).toBeInTheDocument()
  })

  it('high threat teams show warning indicator', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      selectedTeam: 'Team 3',
      teamsState: { 'Team 3': { budget: 100 } },
      teamThreatScores: { 'Team 3': 187 }, // >= 150 threshold
    })

    render(
      <MemoryRouter>
        <TeamRosterPanel />
      </MemoryRouter>
    )

    expect(screen.getByText(/High threat/)).toBeInTheDocument()
    expect(screen.getByText(/\$187 ceiling value/)).toBeInTheDocument()
  })

  it('TeamRosterPanel renders without picks', () => {
    useDraftStore.setState({ phase: 'live', myTeamName: null })

    render(
      <MemoryRouter>
        <TeamRosterPanel />
      </MemoryRouter>
    )

    expect(screen.getByText('Team Rosters')).toBeInTheDocument()
    expect(screen.getByText('No picks yet')).toBeInTheDocument()
  })

  it('selectedTeam defaults to myTeamName', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      selectedTeam: null, // no explicit selection
      teamsState: { Stephen: { budget: 150, slotsUsed: 1, totalSlots: 16 } },
      myRoster: [{ player_id: '1', player_name: 'CMC', position: 'RB', price: 50 }],
    })

    render(
      <MemoryRouter>
        <TeamRosterPanel />
      </MemoryRouter>
    )

    // Dropdown lands on my team, and my roster (with position slots) shows.
    expect(screen.getByLabelText('Select team').value).toBe('Stephen')
    expect(screen.getByText('CMC')).toBeInTheDocument()
  })

  // --- v2: name normalization, position tracking, unified slots, ws resync ---

  it('recordPick removes Amon-Ra by normalized name', () => {
    // Draftboard stores the hyphenated name; the relay sends the spaced DOM name.
    useDraftStore.setState({
      phase: 'live',
      availablePlayers: [
        { id: 'p1', name: 'Amon-Ra St. Brown', position: 'WR', ai_bid_ceiling: 45 },
        { id: 'p2', name: 'CeeDee Lamb', position: 'WR', ai_bid_ceiling: 58 },
      ],
    })

    act(() => {
      useDraftStore.getState().recordPick({
        player_name: 'Amon Ra St. Brown', // spaced, no hyphen/punctuation match
        final_price: 40,
        winner: 'Team 3',
      })
    })

    const remaining = useDraftStore.getState().availablePlayers.map((p) => p.name)
    expect(remaining).toEqual(['CeeDee Lamb']) // Amon-Ra removed despite spelling diff
  })

  it('isYours normalizes both sides', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      availablePlayers: [{ id: 'p1', name: 'CMC', position: 'RB', ai_bid_ceiling: 70 }],
      myRoster: [],
    })

    act(() => {
      useDraftStore.getState().recordPick({
        player_name: 'CMC',
        final_price: 50,
        winner: '  stephen ', // trailing space + lowercase vs 'Stephen'
      })
    })

    // Normalized comparison still recognizes the pick as ours.
    expect(useDraftStore.getState().myRoster.map((p) => p.player_name)).toContain('CMC')
  })

  it('teamPicks stores position from available', () => {
    useDraftStore.setState({
      phase: 'live',
      availablePlayers: [{ id: 'p1', name: 'Bijan Robinson', position: 'RB', ai_bid_ceiling: 60 }],
    })

    act(() => {
      // The relay carries no position — it must come from the available lookup.
      useDraftStore.getState().recordPick({
        player_name: 'Bijan Robinson',
        final_price: 55,
        winner: 'Team 3',
      })
    })

    expect(useDraftStore.getState().teamPicks['Team 3'][0].position).toBe('RB')
  })

  it('assignToSlot used for opponent rosters', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      selectedTeam: 'Team 3',
      teamsState: { 'Team 3': { budget: 100 } },
      teamPicks: {
        'Team 3': [{ player_name: 'Bijan Robinson', price: 55, position: 'RB', ceiling: 60 }],
      },
    })

    render(
      <MemoryRouter>
        <TeamRosterPanel />
      </MemoryRouter>
    )

    // The slot grid renders FLEX/BN/Empty labels the old flat opponent list never did.
    expect(screen.getByText('Bijan Robinson')).toBeInTheDocument()
    expect(screen.getAllByText('FLEX').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('Empty').length).toBeGreaterThanOrEqual(1)
  })

  it('opponent null position goes to BN', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      selectedTeam: 'Team 3',
      teamsState: { 'Team 3': { budget: 100 } },
      teamPicks: {
        'Team 3': [{ player_name: 'Ghost Player', price: 5, position: null, ceiling: null }],
      },
    })

    render(
      <MemoryRouter>
        <TeamRosterPanel />
      </MemoryRouter>
    )

    // A null-position pick falls through to the bench slot.
    const row = screen.getByText('Ghost Player').closest('div')
    expect(row.textContent).toContain('BN')
  })

  it('ws reconnect resyncs recommendation', async () => {
    const draftApi = await import('../api/draft')
    draftApi.getRecommendation.mockResolvedValue({
      type: 'recommendation',
      action: 'buy',
      player_name: 'Puka Nacua',
      bid_ceiling: 50,
      system_value: 40,
      market_value: 45,
      confidence: 'high',
      reasoning: 'Tier 1 WR',
    })

    useDraftStore.setState({ phase: 'live' })
    render(
      <MemoryRouter>
        <DraftRoom />
      </MemoryRouter>
    )

    // Initial connect + onopen resync pulls the recommendation.
    await waitFor(() =>
      expect(useDraftStore.getState().recommendation?.player_name).toBe('Puka Nacua')
    )

    // Simulate a focus-loss reconnect: clear state, re-fire onopen.
    act(() => useDraftStore.setState({ recommendation: null }))
    const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1]
    await act(async () => {
      ws.onopen()
    })

    await waitFor(() =>
      expect(useDraftStore.getState().recommendation?.player_name).toBe('Puka Nacua')
    )

    draftApi.getRecommendation.mockResolvedValue(null) // restore module default
  })

  // --- Snake draft: your_turn, snake_pick, countdown ---

  it('your_turn WS message sets isYourTurn and clears the recommendation', async () => {
    useDraftStore.setState({
      phase: 'live',
      recommendation: { type: 'recommendation', player_name: 'Old', action: 'draft' },
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
          type: 'your_turn',
          payload: { round: 8, pick: 93, picks_until_your_turn: 0 },
        }),
      })
    })

    const s = useDraftStore.getState()
    expect(s.isYourTurn).toBe(true)
    expect(s.currentRound).toBe(8)
    expect(s.currentPick).toBe(93)
    expect(s.picksUntilYourTurn).toBe(0)
    expect(s.recommendation).toBeNull()
  })

  it('your_turn_soon WS message sets the picks countdown', async () => {
    useDraftStore.setState({ phase: 'live' })
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
          type: 'your_turn_soon',
          payload: { picks_until_your_turn: 2, round: 7 },
        }),
      })
    })

    expect(useDraftStore.getState().picksUntilYourTurn).toBe(2)
  })

  it('recordSnakePick removes the player from available and tracks the team', () => {
    useDraftStore.setState({
      phase: 'live',
      isYourTurn: true,
      availablePlayers: [
        { id: 'p1', name: 'J. Dobbins', position: 'RB', yahoo_player_id: 'y1' },
        { id: 'p2', name: 'Travis Kelce', position: 'TE', yahoo_player_id: 'y3' },
      ],
    })

    act(() => {
      useDraftStore.getState().recordSnakePick({
        player_name: 'J. Dobbins',
        yahoo_player_id: 'y1',
        position: 'RB',
        picker: 'Bart',
        pick_number: 84,
        round: 7,
      })
    })

    const s = useDraftStore.getState()
    expect(s.availablePlayers.map((p) => p.name)).toEqual(['Travis Kelce'])
    expect(s.teamPicks['Bart']).toHaveLength(1)
    expect(s.teamPicks['Bart'][0].player_name).toBe('J. Dobbins')
    // A pick ends your turn.
    expect(s.isYourTurn).toBe(false)
  })

  it('recordSnakePick adds to your roster when you are the picker', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      rosterSlotsRemaining: 16,
      myRoster: [],
      availablePlayers: [
        { id: 'p1', name: 'Bijan Robinson', position: 'RB', yahoo_player_id: 'y1' },
      ],
    })

    act(() => {
      useDraftStore.getState().recordSnakePick({
        player_name: 'Bijan Robinson',
        yahoo_player_id: 'y1',
        position: 'RB',
        picker: 'Stephen',
      })
    })

    const s = useDraftStore.getState()
    expect(s.myRoster.map((p) => p.player_name)).toContain('Bijan Robinson')
    expect(s.rosterSlotsRemaining).toBe(15)
  })

  it('recordSnakePick never wipes the whole available list', () => {
    // A pick whose name matches nothing must remove zero players, not all.
    useDraftStore.setState({
      phase: 'live',
      availablePlayers: [
        { id: 'a', name: 'Josh Allen', position: 'QB' },
        { id: 'b', name: 'Bijan Robinson', position: 'RB' },
      ],
    })

    act(() => {
      useDraftStore.getState().recordSnakePick({
        player_name: 'Nobody Here',
        picker: 'Team 3',
      })
    })

    expect(useDraftStore.getState().availablePlayers).toHaveLength(2)
  })

  it('SnakePanel shows YOUR TURN when on the clock', async () => {
    const SnakePanel = (await import('../components/draft/SnakePanel')).default
    useDraftStore.setState({
      phase: 'live',
      isYourTurn: true,
      currentRound: 8,
      currentPick: 93,
    })
    render(
      <MemoryRouter>
        <SnakePanel />
      </MemoryRouter>
    )
    expect(screen.getByText('YOUR TURN')).toBeInTheDocument()
    expect(screen.getByText('Round 8, Pick 93')).toBeInTheDocument()
  })

  it('SnakePanel shows the countdown when you are 2 picks away', async () => {
    const SnakePanel = (await import('../components/draft/SnakePanel')).default
    useDraftStore.setState({
      phase: 'live',
      isYourTurn: false,
      picksUntilYourTurn: 2,
      currentRound: 7,
    })
    render(
      <MemoryRouter>
        <SnakePanel />
      </MemoryRouter>
    )
    expect(screen.getByText("You're up in 2 picks")).toBeInTheDocument()
  })

  it('AvailablePlayers shows adp_rank (not adp_ai) for snake leagues', async () => {
    const { LeagueContext } = await import('../context/LeagueContext')
    const AvailablePlayersCmp = (await import('../components/draft/AvailablePlayers')).default
    useDraftStore.setState({
      phase: 'live',
      availablePlayers: [
        // adp_ai is the tied float (4.0); adp_rank is the clean integer (1).
        { id: 'p1', name: 'Bijan Robinson', position: 'RB', team_abbr: 'ATL',
          adp_ai: 4.0, adp_rank: 1, adp_fantasypros: 2.0, adp_diff: 1.0 },
      ],
    })
    render(
      <MemoryRouter>
        <LeagueContext.Provider
          value={{ isSnake: true, isAuction: false, scoringFormat: 'ppr',
                   selectedLeague: { draft_type: 'snake' }, setSelectedLeague() {} }}
        >
          <AvailablePlayersCmp />
        </LeagueContext.Provider>
      </MemoryRouter>
    )
    // Shows the integer rank, not the raw 4.0 adp_ai.
    expect(screen.getByText('1')).toBeInTheDocument()
    expect(screen.queryByText('4.0')).not.toBeInTheDocument()
  })

  it('rosterSlotsRemaining only decrements on isYours', () => {
    useDraftStore.setState({
      phase: 'live',
      myTeamName: 'Stephen',
      rosterSlotsRemaining: 16,
      myRoster: [],
      availablePlayers: [
        { id: 'p1', name: 'Bijan Robinson', position: 'RB', ai_bid_ceiling: 60 },
        { id: 'p2', name: 'CeeDee Lamb', position: 'WR', ai_bid_ceiling: 58 },
      ],
    })

    // An opponent's pick must NOT touch your slot count.
    act(() => {
      useDraftStore.getState().recordPick({
        player_name: 'Bijan Robinson',
        final_price: 55,
        winner: 'Team 3',
      })
    })
    expect(useDraftStore.getState().rosterSlotsRemaining).toBe(16) // unchanged

    // Your pick decrements by exactly one.
    act(() => {
      useDraftStore.getState().recordPick({
        player_name: 'CeeDee Lamb',
        final_price: 40,
        winner: 'Stephen',
      })
    })
    const s = useDraftStore.getState()
    expect(s.rosterSlotsRemaining).toBe(15) // exactly one decrement
    expect(s.myRoster).toHaveLength(1)
  })
})
