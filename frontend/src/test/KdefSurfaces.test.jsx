import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { buildPositionOptions, DRAFT_FILTER_POSITIONS, SKILL_POSITIONS } from '../lib/constants'
import PositionBadge from '../components/shared/PositionBadge'

describe('K/DEF surfaces (T4 of #3)', () => {
  it('the position-filter dropdown now offers K and DEF', () => {
    const opts = buildPositionOptions()
    const values = opts.map((o) => o.value)
    expect(values).toContain('K')
    expect(values).toContain('DEF')
    // still has the skill positions and the all-entry
    expect(values).toContain('')
    for (const p of ['QB', 'RB', 'WR', 'TE']) expect(values).toContain(p)
  })

  it('SKILL_POSITIONS stays the true 4-skill set; the filter set is separate', () => {
    expect(SKILL_POSITIONS).toEqual(['QB', 'RB', 'WR', 'TE'])
    expect(DRAFT_FILTER_POSITIONS).toContain('K')
    expect(DRAFT_FILTER_POSITIONS).toContain('DEF')
  })

  it('PositionBadge renders K and DEF with their own colors (not the slate fallback)', () => {
    const { rerender } = render(<PositionBadge position="K" />)
    const k = screen.getByText('K')
    expect(k.className).toContain('amber')
    expect(k.className).not.toContain('slate')

    rerender(<PositionBadge position="DEF" />)
    const def_ = screen.getByText('DEF')
    expect(def_.className).toContain('cyan')
    expect(def_.className).not.toContain('slate')
  })

  it('PositionBadge still styles the skill positions', () => {
    render(<PositionBadge position="QB" />)
    expect(screen.getByText('QB').className).toContain('purple')
  })
})
