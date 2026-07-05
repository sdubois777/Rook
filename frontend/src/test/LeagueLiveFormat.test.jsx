import { render, screen } from '@testing-library/react'
import { act } from 'react'
import { describe, it, expect, beforeEach } from 'vitest'
import { LeagueProvider, useLeague } from '../context/LeagueContext'
import { useDraftStore } from '../stores/draft'

// A tiny probe that renders the resolved format flags from the context.
function Probe() {
  const { isSnake, isAuction } = useLeague()
  return (
    <div>
      <span data-testid="snake">{String(isSnake)}</span>
      <span data-testid="auction">{String(isAuction)}</span>
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
})
