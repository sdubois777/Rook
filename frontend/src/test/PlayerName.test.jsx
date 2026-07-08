import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import PlayerName, { PlayerBadges } from '../components/shared/PlayerName'
import PositionBadge from '../components/shared/PositionBadge'
import InjuryBadge from '../components/shared/InjuryBadge'

describe('shared player badge primitive', () => {
  it('PlayerName renders name + position badge + injury badge together', () => {
    render(<PlayerName name="Ladd McConkey" position="WR" injuryStatus="Q" />)
    expect(screen.getByText('Ladd McConkey')).toBeInTheDocument()
    expect(screen.getByText('WR')).toBeInTheDocument()
    expect(screen.getByText('Q')).toBeInTheDocument()
  })

  it('healthy player shows the position badge but NO injury badge', () => {
    const { container } = render(<PlayerBadges position="RB" injuryStatus={null} />)
    expect(screen.getByText('RB')).toBeInTheDocument()
    // only one badge span (position) — no Q/D/O/IR
    expect(container.textContent).toBe('RB')
  })

  it('uses the SHARED palette (unified) — QB is purple everywhere, not the old rose fork', () => {
    const { container } = render(<PositionBadge position="QB" variant="compact" />)
    const badge = container.querySelector('span')
    expect(badge.className).toContain('text-purple-400')  // shared palette
    expect(badge.className).not.toContain('rose')          // fork removed
  })

  it('dense variant keeps the bordered text-xs density (pixel-intact shared surfaces)', () => {
    const { container } = render(<PositionBadge position="WR" variant="dense" />)
    const badge = container.querySelector('span')
    expect(badge.className).toContain('text-xs')
    expect(badge.className).toContain('border')
    expect(badge.className).toContain('text-blue-400')     // WR shared = blue (not sky)
  })

  it('compact variant is borderless text-[10px] (Trade/Waiver row density preserved)', () => {
    const { container } = render(<PositionBadge position="WR" variant="compact" />)
    const badge = container.querySelector('span')
    expect(badge.className).toContain('text-[10px]')
    expect(badge.className).not.toContain('border')
  })

  it('injury badge tracks variant density and colors by code', () => {
    const { container: dense } = render(<InjuryBadge status="O" variant="dense" />)
    expect(dense.querySelector('span').className).toContain('text-xs')
    expect(dense.querySelector('span').className).toContain('text-red-300')
    const { container: compact } = render(<InjuryBadge status="Q" variant="compact" />)
    expect(compact.querySelector('span').className).toContain('text-[10px]')
    expect(compact.querySelector('span').className).toContain('text-amber-300')
  })

  it('dense badges reserve their own space in-flow — never absolute/overlapping', () => {
    // The dense-variant overlap bug was a badge that didn't reserve flex space and
    // landed on the player name. Both badges must be shrink-0 and NOT absolutely
    // positioned so they sit in-flow beside (not on top of) the name.
    const inj = render(<InjuryBadge status="IR" variant="dense" />).container.querySelector('span')
    expect(inj.className).toContain('shrink-0')
    expect(inj.className).not.toContain('absolute')
    const pos = render(<PositionBadge position="QB" variant="dense" />).container.querySelector('span')
    expect(pos.className).toContain('shrink-0')
    expect(pos.className).not.toContain('absolute')
  })
})
