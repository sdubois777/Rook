import { render, screen } from '@testing-library/react'
import { describe, it, expect, beforeEach } from 'vitest'
import { useDraftStore } from '../stores/draft'
import {
  LeagueContext,
  LeagueProvider,
  useLeague,
} from '../context/LeagueContext'
import AvailablePlayers from '../components/draft/AvailablePlayers'
import RecommendationPanel from '../components/draft/RecommendationPanel'
import { getSnakeTargets } from '../components/draft/SuggestedTargets'

const SNAKE = {
  isSnake: true,
  isAuction: false,
  scoringFormat: 'ppr',
  selectedLeague: { id: '1', draft_type: 'snake', scoring: 'ppr' },
  setSelectedLeague() {},
}
const AUCTION = { ...SNAKE, isSnake: false, isAuction: true }

const withLeague = (ui, value) => (
  <LeagueContext.Provider value={value}>{ui}</LeagueContext.Provider>
)

const PLAYER = {
  id: 'p1',
  name: 'Bijan Robinson',
  position: 'RB',
  team_abbr: 'ATL',
  adp_ai: 3.0,
  adp_fantasypros: 4.0,
  ai_bid_ceiling: 60,
  market_value: 50,
  recommended_bid_ceiling: 60,
}

beforeEach(() => {
  localStorage.clear()
  useDraftStore.setState({
    availablePlayers: [],
    availableFilter: { position: '', search: '' },
    recommendation: null,
    myRoster: [],
    myBudget: 200,
    rosterSlotsRemaining: 16,
  })
})

describe('LeagueContext', () => {
  function probe() {
    let captured
    function Probe() {
      captured = useLeague()
      return null
    }
    render(
      <LeagueProvider>
        <Probe />
      </LeagueProvider>
    )
    return () => captured
  }

  it('isSnake true for a snake league', () => {
    localStorage.setItem(
      'selectedLeague',
      JSON.stringify({ id: '1', draft_type: 'snake', scoring: 'ppr' })
    )
    const get = probe()
    expect(get().isSnake).toBe(true)
    expect(get().isAuction).toBe(false)
  })

  it('isAuction true for an auction league', () => {
    localStorage.setItem(
      'selectedLeague',
      JSON.stringify({ id: '2', draft_type: 'auction', scoring: 'half_ppr' })
    )
    const get = probe()
    expect(get().isAuction).toBe(true)
    expect(get().isSnake).toBe(false)
    expect(get().scoringFormat).toBe('half_ppr')
  })
})

describe('AvailablePlayers league toggle', () => {
  it('shows ADP columns for snake', () => {
    useDraftStore.setState({ availablePlayers: [PLAYER] })
    render(withLeague(<AvailablePlayers />, SNAKE))

    expect(screen.getByText('AI ADP')).toBeInTheDocument()
    expect(screen.getByText('FP ADP')).toBeInTheDocument()
    expect(screen.queryByText('Ceiling')).not.toBeInTheDocument()
    expect(screen.getByText('3.0')).toBeInTheDocument() // adp_ai rendered
  })

  it('shows dollar columns for auction', () => {
    useDraftStore.setState({ availablePlayers: [PLAYER] })
    render(withLeague(<AvailablePlayers />, AUCTION))

    expect(screen.getByText('Ceiling')).toBeInTheDocument()
    expect(screen.getByText('Market')).toBeInTheDocument()
    expect(screen.queryByText('AI ADP')).not.toBeInTheDocument()
    expect(screen.getByText('$60')).toBeInTheDocument() // ceiling rendered
  })
})

describe('SuggestedTargets snake ordering', () => {
  it('sorts by ADP ascending for snake', () => {
    const players = [
      { id: 'a', name: 'A', position: 'RB', adp_ai: 30 },
      { id: 'b', name: 'B', position: 'WR', adp_ai: 10 },
      { id: 'c', name: 'C', position: 'TE', adp_ai: 20 },
    ]
    const out = getSnakeTargets([], players)
    expect(out.map((p) => p.name)).toEqual(['B', 'C', 'A'])
  })
})

describe('RecommendationPanel league toggle', () => {
  it('shows ADP target for snake', () => {
    useDraftStore.setState({
      recommendation: {
        action: 'buy',
        player_name: 'Bijan Robinson',
        position: 'RB',
        confidence: 'high',
        bid_ceiling: 60,
        system_value: 55,
        market_value: 50,
        adp_ai: 3,
        adp_fp: 4,
        adp_diff: -1,
      },
    })
    render(withLeague(<RecommendationPanel />, SNAKE))

    expect(screen.getByText('TARGET PICK 3')).toBeInTheDocument()
    expect(screen.getByText(/AI ADP/)).toBeInTheDocument()
    // Auction action label is gone in snake mode.
    expect(screen.queryByText('BUY')).not.toBeInTheDocument()
  })
})
