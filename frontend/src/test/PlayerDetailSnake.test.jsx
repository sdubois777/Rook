import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LeagueContext } from '../context/LeagueContext'
import PlayerDetailPanel from '../components/PlayerDetailPanel'

// Hoisted so the vi.mock factory (also hoisted) can reference it.
// Deliberately still carries recommended_bid_ceiling / baseline_value / ceiling_value /
// floor_value / prior_season_price: the panel must IGNORE them now, and a fixture that
// dropped them could not tell the difference between "not rendered" and "not supplied".
const PLAYER = vi.hoisted(() => ({
  id: 'p1', name: 'Bijan Robinson', position: 'RB', team_abbr: 'ATL', age: 23,
  tier: 1, situation_score: 'strong',
  recommended_bid_ceiling: 80, ai_bid_ceiling: 80, baseline_value: 70, market_value: 65,
  market_value_season: 2026,
  ai_confidence_floor: 70, ai_confidence_ceiling: 90, ceiling_value: 90, floor_value: 60,
  prior_season_price: 55, prior_season_year: 2025,
  adp_rank: 1, adp_fantasypros: 1.5, adp_diff: 0.5, snake_flag: 'TARGET',
  value_assessment: 'good_value',
  auction_note: 'Workhorse back with elite usage.',
  profile: { clean_season_baseline: { projected_ppr_season: 300 }, confidence: 'high' },
  flags: [], dependencies: [], beat_signals: [],
}))

vi.mock('../api/players', () => ({
  fetchPlayer: vi.fn(() => Promise.resolve(h.player)),
}))

const h = vi.hoisted(() => ({ player: null }))

function renderPanel(isSnake, overrides = {}) {
  h.player = { ...PLAYER, ...overrides }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <LeagueContext.Provider
        value={{
          isSnake,
          isAuction: !isSnake,
          draftType: isSnake ? 'snake' : 'auction',
          scoringFormat: 'ppr',
          selectedLeague: { draft_type: isSnake ? 'snake' : 'auction' },
          setSelectedLeague() {},
          formatOverride: null,
          setFormatOverride() {},
          canChooseFormat: false,
        }}
      >
        <PlayerDetailPanel playerId="p1" />
      </LeagueContext.Provider>
    </QueryClientProvider>
  )
}

describe('PlayerDetailPanel league toggle', () => {
  it('hides dollar values and shows adp_rank + flag for snake', async () => {
    renderPanel(true)
    expect(await screen.findByText('AI ADP')).toBeInTheDocument()
    expect(screen.getByText('#1')).toBeInTheDocument() // adp_rank
    expect(screen.getByText('TARGET')).toBeInTheDocument() // snake_flag
    expect(screen.queryByText('Bid Ceiling')).not.toBeInTheDocument()
    expect(screen.queryByText('AI Ceiling')).not.toBeInTheDocument()
    expect(screen.queryByText('Confidence Range')).not.toBeInTheDocument()
  })

  it('shows the AI ceiling for auction', async () => {
    renderPanel(false)
    expect(await screen.findByText('AI Ceiling')).toBeInTheDocument()
    expect(screen.getByText('$80')).toBeInTheDocument()
    expect(screen.queryByText('AI ADP')).not.toBeInTheDocument()
  })
})

describe('PlayerDetailPanel — the numbers that were cut', () => {
  // "Too many numbers, and most of them don't matter." These five were intermediate
  // values nobody bids off, two of them printed twice on the same panel.
  it.each(['Bid Ceiling', 'System', 'Ceiling', 'Floor'])(
    'no longer shows the %s stat box', async (label) => {
      renderPanel(false)
      await screen.findByText('AI Ceiling')
      // Exact match: "Ceiling"/"Floor" also appear in Projection as PPR range labels
      // ("Floor: 225"), and those must survive — only the dollar boxes are gone.
      expect(screen.queryByText(label, { exact: true })).not.toBeInTheDocument()
    })

  it('no longer shows the prior-season Avg Price row', async () => {
    renderPanel(false)
    await screen.findByText('AI Ceiling')
    expect(screen.queryByText(/Avg Price/)).not.toBeInTheDocument()
  })

  it('no longer renders the System/Market comparison bar', async () => {
    renderPanel(false)
    await screen.findByText('AI Ceiling')
    // The bar printed "System: $70" / "Market: $65" — the same figures removed above.
    expect(screen.queryByText(/System: \$/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Market: \$/)).not.toBeInTheDocument()
  })

  it('keeps the three that matter', async () => {
    renderPanel(false)
    expect(await screen.findByText('AI Ceiling')).toBeInTheDocument()
    expect(screen.getByText('2026 ADP')).toBeInTheDocument()
    expect(screen.getByText('Confidence Range')).toBeInTheDocument()
  })
})

describe('PlayerDetailPanel — section order', () => {
  function orderOf(...titles) {
    const nodes = titles.map((t) => screen.getByText(t))
    return nodes.every((node, i) => {
      if (i === 0) return true
      const prev = nodes[i - 1]
      // DOCUMENT_POSITION_FOLLOWING === 4
      return (prev.compareDocumentPosition(node) & 4) !== 0
    })
  }

  it('puts Projection above the AI prose, below the Valuation numbers', async () => {
    renderPanel(false)
    await screen.findByText('AI Ceiling')
    expect(orderOf('Valuation', 'Projection', 'AI Assessment')).toBe(true)
  })

  it('same order for snake', async () => {
    renderPanel(true)
    await screen.findByText('AI ADP')
    expect(orderOf('Valuation', 'Projection', 'AI Assessment')).toBe(true)
  })
})

describe('PlayerDetailPanel — the AI Assessment heading is never empty', () => {
  it('is omitted for an auction player with no assessment and no tactical flags', async () => {
    renderPanel(false, {
      value_assessment: null, pay_up_flag: false, nomination_target_flag: false,
    })
    await screen.findByText('AI Ceiling')
    // A hoisted heading over inner conditions would render a bare title over nothing.
    expect(screen.queryByText('AI Assessment')).not.toBeInTheDocument()
  })

  it('is present for a snake player with a flag but no prose', async () => {
    renderPanel(true, { auction_note: null })
    await screen.findByText('AI ADP')
    expect(screen.getByText('AI Assessment')).toBeInTheDocument()
    expect(screen.getByText('TARGET')).toBeInTheDocument()
  })

  it('is omitted for a snake player with neither flag nor prose', async () => {
    renderPanel(true, { auction_note: null, snake_flag: null })
    await screen.findByText('AI ADP')
    expect(screen.queryByText('AI Assessment')).not.toBeInTheDocument()
  })
})
