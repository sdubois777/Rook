import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'fs'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LeagueContext } from '../context/LeagueContext'
import DraftBoard from '../pages/DraftBoard'

// DraftBoard gates its query on Clerk's isLoaded — mock useAuth as ready.
vi.mock('@clerk/clerk-react', () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: true, getToken: async () => 'tok' }),
}))

vi.mock('../api/draftboard', () => ({
  fetchDraftboard: vi.fn().mockResolvedValue({
    tiers: {
      1: [
        {
          id: 'p1', name: 'Bijan Robinson', position: 'RB', team_abbr: 'ATL', tier: 1,
          ai_bid_ceiling: 80, market_value: 50, recommended_bid_ceiling: 80, ppr_points: 300,
          adp_ai: 3.0, adp_fantasypros: 5, adp_scoring: 'ppr', value_assessment: 'good_value',
          adp_rank: 1, adp_diff: -4, snake_flag: 'TARGET', round_num: 1,
        },
        {
          id: 'p2', name: 'Josh Allen', position: 'QB', team_abbr: 'BUF', tier: 1,
          ai_bid_ceiling: 48, market_value: 27, recommended_bid_ceiling: 48, ppr_points: 400,
          adp_ai: 28.0, adp_fantasypros: 9, adp_scoring: 'ppr',
          adp_rank: 14, adp_diff: -5, snake_flag: 'VALUE', round_num: 2,
        },
      ],
    },
    total_players: 2,
  }),
}))

function renderBoard(isSnake, teamCount = 12) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const value = {
    isSnake,
    isAuction: !isSnake,
    scoringFormat: 'ppr',
    selectedLeague: { draft_type: isSnake ? 'snake' : 'auction', team_count: teamCount },
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
    // headers repeat per round group, so allow multiples
    expect((await screen.findAllByText('AI ADP')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('FP ADP').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Diff').length).toBeGreaterThan(0)
    expect(screen.queryByText('AI Ceil')).not.toBeInTheDocument()
    // FP ADP value rendered (Bijan fp_rank 5, unique)
    expect(screen.getByText('5')).toBeInTheDocument()
  })

  it('shows ceiling columns when auction', async () => {
    renderBoard(false)
    expect(await screen.findByText('AI Ceil')).toBeInTheDocument()
    expect(screen.getByText('PPR')).toBeInTheDocument()
    expect(screen.queryByText('AI ADP')).not.toBeInTheDocument()
  })

  it('groups by round and shows snake flag badges when snake', async () => {
    renderBoard(true)
    // round headers (Bijan rank 1 -> Round 1; Allen rank 14 -> Round 2 @ 12-team)
    expect(await screen.findByText('Round 1')).toBeInTheDocument()
    expect(screen.getByText('Round 2')).toBeInTheDocument()
    // snake flag badges (not the auction PAY UP/NOMINATE)
    expect(screen.getByText('TARGET')).toBeInTheDocument()
    expect(screen.getByText('VALUE')).toBeInTheDocument()
  })

  it('snake orders by adp_rank ascending', async () => {
    renderBoard(true)
    const bijan = await screen.findByText('Bijan Robinson') // adp_rank 1
    const allen = screen.getByText('Josh Allen') // adp_rank 14
    expect(
      bijan.compareDocumentPosition(allen) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy()
  })

  it('floors teamCount to 8 for a single-team test league', async () => {
    // team_count=1 would put adp_rank 14 in Round 14 (every player its own
    // round). Floored to 8, ceil(14/8)=2 — Allen lands in Round 2, not Round 14.
    renderBoard(true, 1)
    expect(await screen.findByText('Round 1')).toBeInTheDocument()
    expect(screen.getByText('Round 2')).toBeInTheDocument()
    expect(screen.queryByText('Round 14')).not.toBeInTheDocument()
  })

  it('uses the actual team count for real leagues', async () => {
    // 16-team league: ceil(14/16)=1 — both players collapse into Round 1.
    renderBoard(true, 16)
    expect(await screen.findByText('Round 1')).toBeInTheDocument()
    expect(screen.queryByText('Round 2')).not.toBeInTheDocument()
  })

  it('hides the auction budget header for snake leagues', async () => {
    renderBoard(true)
    await screen.findByText('Round 1')
    expect(screen.queryByText(/Budget: \$200/)).not.toBeInTheDocument()
  })

  it('shows the auction budget header for auction leagues', async () => {
    renderBoard(false)
    expect(await screen.findByText(/Budget: \$200/)).toBeInTheDocument()
  })
})

describe('App LeagueProvider structure', () => {
  it('wraps the router once, not twice', () => {
    // Read the App source to guard against the dual-provider regression. Tolerate
    // being run from frontend/ (CI) or the repo root.
    let src
    try {
      src = readFileSync('src/App.jsx', 'utf-8')
    } catch {
      src = readFileSync('frontend/src/App.jsx', 'utf-8')
    }
    const count = (src.match(/<LeagueProvider>/g) || []).length
    expect(count).toBe(1)
  })
})
