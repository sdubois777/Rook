import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LeagueContext } from '../context/LeagueContext'
import PlayerDetailPanel from '../components/PlayerDetailPanel'

// Hoisted so the vi.mock factory (also hoisted) can reference it.
const PLAYER = vi.hoisted(() => ({
  id: 'p1', name: 'Bijan Robinson', position: 'RB', team_abbr: 'ATL', age: 23,
  tier: 1, situation_score: 'strong',
  recommended_bid_ceiling: 80, ai_bid_ceiling: 80, baseline_value: 70, market_value: 65,
  ai_confidence_floor: 70, ai_confidence_ceiling: 90, ceiling_value: 90, floor_value: 60,
  adp_rank: 1, adp_fantasypros: 1.5, adp_diff: 0.5, snake_flag: 'TARGET',
  auction_note: 'Workhorse back with elite usage.',
  flags: [], dependencies: [], beat_signals: [],
}))

vi.mock('../api/players', () => ({
  fetchPlayer: vi.fn().mockResolvedValue(PLAYER),
}))

function renderPanel(isSnake) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <LeagueContext.Provider
        value={{
          isSnake,
          isAuction: !isSnake,
          scoringFormat: 'ppr',
          selectedLeague: { draft_type: isSnake ? 'snake' : 'auction' },
          setSelectedLeague() {},
        }}
      >
        <PlayerDetailPanel playerId="p1" />
      </LeagueContext.Provider>
    </QueryClientProvider>
  )
}

describe('PlayerDetailPanel league toggle', () => {
  it('hides bid ceiling and shows adp_rank + flag for snake', async () => {
    renderPanel(true)
    expect(await screen.findByText('AI ADP')).toBeInTheDocument()
    expect(screen.getByText('#1')).toBeInTheDocument() // adp_rank
    expect(screen.getByText('TARGET')).toBeInTheDocument() // snake_flag
    expect(screen.queryByText('Bid Ceiling')).not.toBeInTheDocument()
    expect(screen.queryByText('AI Ceiling')).not.toBeInTheDocument()
    expect(screen.queryByText('Confidence Range')).not.toBeInTheDocument()
  })

  it('shows bid ceiling for auction', async () => {
    renderPanel(false)
    expect(await screen.findByText('Bid Ceiling')).toBeInTheDocument()
    expect(screen.queryByText('AI ADP')).not.toBeInTheDocument()
  })
})
