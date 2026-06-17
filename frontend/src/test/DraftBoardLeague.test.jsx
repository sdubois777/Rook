import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'fs'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LeagueContext } from '../context/LeagueContext'
import DraftBoard from '../pages/DraftBoard'

vi.mock('../api/draftboard', () => ({
  fetchDraftboard: vi.fn().mockResolvedValue({
    tiers: {
      1: [
        {
          id: 'p1', name: 'Bijan Robinson', position: 'RB', team_abbr: 'ATL', tier: 1,
          ai_bid_ceiling: 80, market_value: 50, recommended_bid_ceiling: 80, ppr_points: 300,
          adp_ai: 3.0, adp_fantasypros: 1.5, adp_scoring: 'ppr', value_assessment: 'good_value',
        },
        {
          id: 'p2', name: 'Josh Allen', position: 'QB', team_abbr: 'BUF', tier: 1,
          ai_bid_ceiling: 48, market_value: 27, recommended_bid_ceiling: 48, ppr_points: 400,
          adp_ai: 28.0, adp_fantasypros: 27.5, adp_scoring: 'ppr',
        },
      ],
    },
    total_players: 2,
  }),
}))

function renderBoard(isSnake) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const value = {
    isSnake,
    isAuction: !isSnake,
    scoringFormat: 'ppr',
    selectedLeague: { draft_type: isSnake ? 'snake' : 'auction' },
    setSelectedLeague() {},
  }
  return render(
    <QueryClientProvider client={qc}>
      <LeagueContext.Provider value={value}>
        <DraftBoard />
      </LeagueContext.Provider>
    </QueryClientProvider>
  )
}

describe('DraftBoard league toggle', () => {
  it('reads isSnake from context and shows ADP columns when snake', async () => {
    renderBoard(true)
    expect(await screen.findByText('AI ADP')).toBeInTheDocument()
    expect(screen.getByText('FP ADP')).toBeInTheDocument()
    expect(screen.getByText('Diff')).toBeInTheDocument()
    expect(screen.queryByText('AI Ceil')).not.toBeInTheDocument()
    // adp_ai value rendered (Bijan 3.0)
    expect(screen.getByText('3.0')).toBeInTheDocument()
  })

  it('shows ceiling columns when auction', async () => {
    renderBoard(false)
    expect(await screen.findByText('AI Ceil')).toBeInTheDocument()
    expect(screen.getByText('PPR')).toBeInTheDocument()
    expect(screen.queryByText('AI ADP')).not.toBeInTheDocument()
  })

  it('snake sorts by adp_ai ascending', async () => {
    renderBoard(true)
    const bijan = await screen.findByText('Bijan Robinson') // adp_ai 3
    const allen = screen.getByText('Josh Allen') // adp_ai 28
    // Bijan must precede Allen in document order.
    expect(
      bijan.compareDocumentPosition(allen) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy()
  })
})

describe('App LeagueProvider structure', () => {
  it('wraps the router once, not twice', () => {
    // vitest runs from the frontend/ dir; read the App source to guard against
    // the dual-provider regression returning.
    const src = readFileSync('src/App.jsx', 'utf-8')
    const count = (src.match(/<LeagueProvider>/g) || []).length
    expect(count).toBe(1)
  })
})
