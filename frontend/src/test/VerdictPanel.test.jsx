import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import VerdictPanel from '../components/trade/VerdictPanel'

function makeVerdict(overrides = {}) {
  return {
    my_team_id: 'me',
    winner: 'you',
    fairness: 'lopsided you',
    value_delta: 40,
    give_value: 20,
    get_value: 60,
    confidence: 'full',
    hedged: false,
    hedge_reason: '',
    give: [{ id: 'g', name: 'Give Guy', position: 'WR', forward_value: 20, value_trend: 'stable', confidence: 'full', buy_low: false, sell_high: false, why: 'steady' }],
    get: [{ id: 'x', name: 'Get Guy', position: 'WR', forward_value: 60, value_trend: 'rising', confidence: 'full', buy_low: true, sell_high: false, why: 'rising usage' }],
    roster_guard: { triggered: false, net_players: 0, open_slots: 5, drop_recommendations: [], message: '' },
    rationale: 'Clear upgrade.',
    demo_mode: true,
    ...overrides,
  }
}

describe('VerdictPanel — confidence/hedge visibility', () => {
  it('a confident full-confidence verdict is NOT tentative', () => {
    render(<VerdictPanel verdict={makeVerdict()} />)
    expect(screen.getByText('You win')).toBeInTheDocument()
    expect(screen.queryByText('tentative')).not.toBeInTheDocument()
    expect(screen.queryByText(/Caveat:/)).not.toBeInTheDocument()
  })

  it('a hedged (team-change/limited) verdict renders tentative with the caveat shown', () => {
    const hedged = makeVerdict({
      fairness: 'lean you',           // backend downgraded from lopsided
      confidence: 'limited',
      hedged: true,
      hedge_reason: 'Cooks: limited (team change within last-5 window)',
    })
    render(<VerdictPanel verdict={hedged} />)
    // tentative chip is shown, and the hedge_reason is surfaced (not buried)
    expect(screen.getByText('tentative')).toBeInTheDocument()
    expect(screen.getByText(/Caveat:/)).toBeInTheDocument()
    expect(screen.getByText(/team change within last-5 window/)).toBeInTheDocument()
  })

  it('renders the roster-guard warning + drop recs only when triggered', () => {
    const guarded = makeVerdict({
      roster_guard: {
        triggered: true, net_players: 1, open_slots: 0,
        drop_recommendations: [{ id: 'b', name: 'Benchwarmer', forward_value: 5 }],
        message: 'You receive 2 and give 1 (net +1) but have 0 open slots.',
      },
    })
    render(<VerdictPanel verdict={guarded} />)
    expect(screen.getByText(/net \+1/)).toBeInTheDocument()
    expect(screen.getByText(/Benchwarmer/)).toBeInTheDocument()
  })

  it('renders exactly the backend value_delta (never re-derived)', () => {
    render(<VerdictPanel verdict={makeVerdict({ value_delta: 17 })} />)
    expect(screen.getByText('+17')).toBeInTheDocument()
  })
})
