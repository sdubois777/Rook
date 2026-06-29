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

describe('VerdictPanel — acceptability read (the other side)', () => {
  it('"likely to accept" renders as a positive read with the grounded why', () => {
    const v = makeVerdict({
      acceptability: { verdict: 'likely_accept', their_net: 3.7, overtake_flag: false,
        hedged: false, why: 'P-rm4 fills a RB need on their roster' },
    })
    render(<VerdictPanel verdict={v} />)
    expect(screen.getByText('Likely to accept')).toBeInTheDocument()
    expect(screen.getByText(/fills a RB need/)).toBeInTheDocument()
    expect(screen.getByText('+3.7')).toBeInTheDocument()      // their gain, exact
  })

  it('a trade you WIN but they would reject does NOT look like a win', () => {
    // winner=you (the your-side header) but the acceptability section is a
    // clear caution — "Likely to reject", their gain negative.
    const v = makeVerdict({
      winner: 'you', fairness: 'lopsided you',
      acceptability: { verdict: 'likely_reject', their_net: -19, overtake_flag: false,
        hedged: false, why: "they're set at RB — P-scrub adds little for them" },
    })
    render(<VerdictPanel verdict={v} />)
    expect(screen.getByText('You win')).toBeInTheDocument()      // your-side verdict
    expect(screen.getByText('Likely to reject')).toBeInTheDocument()  // their-side caution
    expect(screen.getByText('-19')).toBeInTheDocument()          // their loss, not rounded up
    expect(screen.getByText(/adds little for them/)).toBeInTheDocument()
  })

  it('"may haggle" renders tentatively', () => {
    const v = makeVerdict({
      acceptability: { verdict: 'marginal', their_net: 2.6, overtake_flag: false,
        hedged: false, why: 'P-rm4 is a modest WR upgrade for them; they may haggle' },
    })
    render(<VerdictPanel verdict={v} />)
    expect(screen.getByText('May haggle')).toBeInTheDocument()
    expect(screen.getByText(/may haggle/)).toBeInTheDocument()
  })

  it('surfaces the overtake flag when the trade helps them more on the field', () => {
    const v = makeVerdict({
      acceptability: { verdict: 'likely_accept', their_net: 3.7, overtake_flag: true,
        hedged: false, why: 'P-rm4 fills a RB need on their roster' },
    })
    render(<VerdictPanel verdict={v} />)
    expect(screen.getByText(/let their lineup overtake yours/)).toBeInTheDocument()
  })

  it('a hedged read shows the tentative chip on the acceptability section', () => {
    const v = makeVerdict({
      acceptability: { verdict: 'likely_accept', their_net: 3.7, overtake_flag: false,
        hedged: true, why: 'P-rm4 fills a RB need on their roster (tentative — limited data on a player involved)' },
    })
    render(<VerdictPanel verdict={v} />)
    expect(screen.getByText('Likely to accept')).toBeInTheDocument()
    expect(screen.getByText(/limited data on a player/)).toBeInTheDocument()
  })

  it('renders nothing for the acceptability section when the field is absent', () => {
    render(<VerdictPanel verdict={makeVerdict({ acceptability: undefined })} />)
    expect(screen.queryByText('Likely to accept')).not.toBeInTheDocument()
    expect(screen.queryByText('Likely to reject')).not.toBeInTheDocument()
  })
})
