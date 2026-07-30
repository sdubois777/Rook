import { render, screen } from '@testing-library/react'
import { describe, it, expect, beforeEach } from 'vitest'
import { useDraftStore } from '../stores/draft'
import { LeagueContext } from '../context/LeagueContext'
import SuggestedTargets, { getSuggestedTargets } from '../components/draft/SuggestedTargets'

// SuggestedTargets branches on isSnake, and the value arrow asserted below is the AUCTION
// rendering. This used to rely on the bare context default happening to be auction; now
// that the app default is snake, the intent has to be stated. Wrapping also documents
// which mode the assertion is about.
const AUCTION_CTX = {
  selectedLeague: null,
  setSelectedLeague: () => {},
  draftType: 'auction',
  isSnake: false,
  isAuction: true,
  scoringFormat: 'ppr',
  formatOverride: null,
  setFormatOverride: () => {},
  canChooseFormat: true,
}

function renderAuction(ui) {
  return render(<LeagueContext.Provider value={AUCTION_CTX}>{ui}</LeagueContext.Provider>)
}

const AVAILABLE = [
  { id: 'q1', name: 'Josh Allen', position: 'QB', ai_bid_ceiling: 35, market_value: 33 },
  { id: 'r1', name: 'Bijan Robinson', position: 'RB', ai_bid_ceiling: 60, market_value: 50 }, // gap 10
  { id: 'r2', name: 'Jahmyr Gibbs', position: 'RB', ai_bid_ceiling: 55, market_value: 54 },
  { id: 'w1', name: 'CeeDee Lamb', position: 'WR', ai_bid_ceiling: 58, market_value: 57 },
  { id: 't1', name: 'Sam LaPorta', position: 'TE', ai_bid_ceiling: 25, market_value: 24 },
  { id: 'k1', name: 'Harrison Butker', position: 'K', ai_bid_ceiling: 2, market_value: 2 },
]

describe('getSuggestedTargets', () => {
  it('returns needed positions', () => {
    // Empty roster needs everything — all available starter-position players qualify.
    const out = getSuggestedTargets([], AVAILABLE, 200, 16)
    const names = out.map((p) => p.name)
    expect(names).toContain('Bijan Robinson')
    expect(names).toContain('Josh Allen')
    expect(names).toContain('Sam LaPorta')
  })

  it('respects budget', () => {
    // spendable = 10 - (slotsLeft-1). With budget 10 and 1 slot left -> spendable 10.
    const out = getSuggestedTargets([], AVAILABLE, 10, 1)
    // Only players with ceiling <= 10 survive.
    expect(out.every((p) => p.ai_bid_ceiling <= 10)).toBe(true)
    expect(out.map((p) => p.name)).toContain('Harrison Butker')
    expect(out.map((p) => p.name)).not.toContain('Bijan Robinson')
  })

  it('max 8 results', () => {
    const many = Array.from({ length: 20 }, (_, i) => ({
      id: `p${i}`,
      name: `RB ${i}`,
      position: 'RB',
      ai_bid_ceiling: 50 - i,
      market_value: 40,
    }))
    expect(getSuggestedTargets([], many, 200, 16)).toHaveLength(8)
  })

  it('excludes filled starters', () => {
    // One QB on the roster fills the single QB starter slot -> QB excluded.
    const roster = [{ position: 'QB', player_name: 'Josh Allen', price: 35 }]
    const out = getSuggestedTargets(roster, AVAILABLE, 200, 15)
    expect(out.map((p) => p.position)).not.toContain('QB')
    // RB/WR/TE still needed (FLEX + their own slots).
    expect(out.map((p) => p.position)).toContain('RB')
  })
})

describe('SuggestedTargets component', () => {
  beforeEach(() => {
    useDraftStore.setState({
      myRoster: [],
      availablePlayers: AVAILABLE,
      myBudget: 200,
      rosterSlotsRemaining: 16,
    })
  })

  it('shows value arrow when gap > 5', () => {
    renderAuction(<SuggestedTargets />)
    // Bijan (ceiling 60 - market 50 = gap 10 > 5) renders the up arrow.
    const row = screen.getByText('Bijan Robinson').closest('div')
    expect(row.textContent).toContain('↑')
    // Gibbs (55 - 54 = 1) does not.
    const gibbsRow = screen.getByText('Jahmyr Gibbs').closest('div')
    expect(gibbsRow.textContent).not.toContain('↑')
  })

  it('shows empty state when no players available', () => {
    useDraftStore.setState({ availablePlayers: [] })
    renderAuction(<SuggestedTargets />)
    expect(screen.getByText('No targets available')).toBeInTheDocument()
  })
})
