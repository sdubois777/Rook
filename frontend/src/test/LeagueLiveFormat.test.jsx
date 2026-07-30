import { render, screen } from '@testing-library/react'
import { act } from 'react'
import { describe, it, expect, beforeEach } from 'vitest'
import { LeagueProvider, useLeague } from '../context/LeagueContext'
import { useDraftStore } from '../stores/draft'

// A tiny probe that renders the resolved format flags from the context.
function Probe() {
  const { isSnake, isAuction, scoringFormat, canChooseFormat } = useLeague()
  return (
    <div>
      <span data-testid="snake">{String(isSnake)}</span>
      <span data-testid="auction">{String(isAuction)}</span>
      <span data-testid="scoring">{String(scoringFormat)}</span>
      <span data-testid="canChoose">{String(canChooseFormat)}</span>
    </div>
  )
}

function renderWithLeague(selected) {
  if (selected) localStorage.setItem('selectedLeague', JSON.stringify(selected))
  else localStorage.removeItem('selectedLeague')
  return render(
    <LeagueProvider>
      <Probe />
    </LeagueProvider>
  )
}

describe('LeagueContext — live draft format is the single source of truth', () => {
  beforeEach(() => {
    useDraftStore.setState({ liveDraftType: null })
    localStorage.clear()
  })

  it('falls back to the selected league before any live event', () => {
    renderWithLeague({ draft_type: 'auction' })
    expect(screen.getByTestId('auction').textContent).toBe('true')
    expect(screen.getByTestId('snake').textContent).toBe('false')
  })

  it('live AUCTION overrides a snake-selected league (auction-under-snake self-corrects)', () => {
    renderWithLeague({ draft_type: 'snake' })
    expect(screen.getByTestId('snake').textContent).toBe('true') // before the event
    act(() => useDraftStore.getState().setLiveDraftType('auction'))
    expect(screen.getByTestId('auction').textContent).toBe('true') // live wins
    expect(screen.getByTestId('snake').textContent).toBe('false')
  })

  it('live SNAKE overrides an auction-selected league (symmetric)', () => {
    renderWithLeague({ draft_type: 'auction' })
    expect(screen.getByTestId('auction').textContent).toBe('true')
    act(() => useDraftStore.getState().setLiveDraftType('snake'))
    expect(screen.getByTestId('snake').textContent).toBe('true') // live wins
    expect(screen.getByTestId('auction').textContent).toBe('false')
  })

  it('isSnake and isAuction are exact complements, never both false', () => {
    // The old resolution left BOTH false when no league was synced, and every consumer
    // branches `isSnake ? snake : auction` — which is how auction became the silent
    // default for a no-league user without anyone deciding it.
    for (const league of [null, { draft_type: 'snake' }, { draft_type: 'auction' }]) {
      localStorage.clear()
      const { unmount } = renderWithLeague(league)
      const snake = screen.getByTestId('snake').textContent
      const auction = screen.getByTestId('auction').textContent
      expect(snake).not.toBe(auction)
      unmount()
    }
  })
})

describe('LeagueContext — no synced league defaults to snake PPR', () => {
  beforeEach(() => {
    useDraftStore.setState({ liveDraftType: null })
    localStorage.clear()
  })

  function renderWithOverride(override, selected) {
    if (override) localStorage.setItem('leagueFormatOverride', JSON.stringify(override))
    if (selected) localStorage.setItem('selectedLeague', JSON.stringify(selected))
    return render(
      <LeagueProvider>
        <Probe />
      </LeagueProvider>
    )
  }

  it('defaults to snake + ppr with nothing synced and nothing chosen', () => {
    renderWithOverride(null, null)
    expect(screen.getByTestId('snake').textContent).toBe('true')
    expect(screen.getByTestId('auction').textContent).toBe('false')
    expect(screen.getByTestId('scoring').textContent).toBe('ppr')
    expect(screen.getByTestId('canChoose').textContent).toBe('true')
  })

  it('honours the manual override when no league is synced', () => {
    renderWithOverride({ draft_type: 'auction', scoring: 'half_ppr' }, null)
    expect(screen.getByTestId('auction').textContent).toBe('true')
    expect(screen.getByTestId('scoring').textContent).toBe('half_ppr')
  })

  it('a SYNCED LEAGUE BEATS the manual override (the toggle is no-league-only)', () => {
    renderWithOverride(
      { draft_type: 'auction', scoring: 'standard' },
      { draft_type: 'snake', scoring: 'ppr' }
    )
    expect(screen.getByTestId('snake').textContent).toBe('true')
    expect(screen.getByTestId('scoring').textContent).toBe('ppr')
    // …and the control must not be offered, or it would claim authority it doesn't have.
    expect(screen.getByTestId('canChoose').textContent).toBe('false')
  })

  it('the live draft still beats the override', () => {
    renderWithOverride({ draft_type: 'snake' }, null)
    expect(screen.getByTestId('snake').textContent).toBe('true')
    act(() => useDraftStore.getState().setLiveDraftType('auction'))
    expect(screen.getByTestId('auction').textContent).toBe('true')
  })

  it('survives unparseable localStorage rather than crashing the app', () => {
    localStorage.setItem('leagueFormatOverride', '{not json')
    render(
      <LeagueProvider>
        <Probe />
      </LeagueProvider>
    )
    expect(screen.getByTestId('snake').textContent).toBe('true')
  })
})
