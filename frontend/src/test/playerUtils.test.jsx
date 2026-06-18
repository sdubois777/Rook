import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'fs'
import { join } from 'path'
import {
  getDisplayAdp,
  getFpAdp,
  getAdpDiff,
  getBidCeiling,
  getPrimaryValue,
  formatAdp,
  formatFpAdp,
  formatAdpDiff,
  snakeSortComparator,
  auctionSortComparator,
  getSnakeFlag,
  getSnakeFlagClass,
  getSnakeFlagLabel,
  getRecAdp,
  getRecFpAdp,
  getRecAdpDiff,
} from '../utils/playerUtils'

// adp_ai (4.0, tied) vs adp_rank (1, clean) — the bug this module prevents.
const PLAYER = { adp_ai: 4.0, adp_rank: 1, adp_fantasypros: 2.0, adp_diff: 3.0, ai_bid_ceiling: 60, snake_flag: 'VALUE' }

describe('playerUtils — ADP selection', () => {
  it('getDisplayAdp returns adp_rank, not adp_ai', () => {
    expect(getDisplayAdp(PLAYER)).toBe(1)
    expect(getDisplayAdp(PLAYER)).not.toBe(4.0)
  })

  it('getDisplayAdp returns null when no adp_rank', () => {
    expect(getDisplayAdp({ adp_ai: 4.0 })).toBeNull()
    expect(getDisplayAdp(null)).toBeNull()
  })

  it('getFpAdp returns adp_fantasypros', () => {
    expect(getFpAdp(PLAYER)).toBe(2.0)
  })

  it('getAdpDiff returns adp_diff', () => {
    expect(getAdpDiff(PLAYER)).toBe(3.0)
  })

  it('getBidCeiling returns ai_bid_ceiling', () => {
    expect(getBidCeiling(PLAYER)).toBe(60)
  })

  it('getPrimaryValue returns adp_rank for snake, ceiling for auction', () => {
    expect(getPrimaryValue(PLAYER, true)).toBe(1) // adp_rank
    expect(getPrimaryValue(PLAYER, false)).toBe(60) // ai_bid_ceiling
  })
})

describe('playerUtils — formatting', () => {
  it('formatAdp returns a clean integer string', () => {
    expect(formatAdp(PLAYER)).toBe('1')
  })

  it('formatAdp returns -- when no rank', () => {
    expect(formatAdp({ adp_ai: 4.0 })).toBe('--')
  })

  it('formatFpAdp returns an integer string (FP is a rank)', () => {
    expect(formatFpAdp(PLAYER)).toBe('2')
    expect(formatFpAdp({})).toBe('--')
  })

  it('formatAdpDiff returns +3 for positive', () => {
    expect(formatAdpDiff({ adp_diff: 3 })).toBe('+3')
  })

  it('formatAdpDiff returns -7 for negative', () => {
    expect(formatAdpDiff({ adp_diff: -7 })).toBe('-7')
  })

  it('formatAdpDiff returns 0 and -- correctly', () => {
    expect(formatAdpDiff({ adp_diff: 0 })).toBe('0')
    expect(formatAdpDiff({})).toBe('--')
  })
})

describe('playerUtils — sorting', () => {
  it('snakeSortComparator sorts by adp_rank ascending, nulls last', () => {
    const players = [
      { name: 'C', adp_rank: 3 },
      { name: 'A', adp_rank: 1 },
      { name: 'N', adp_rank: null },
      { name: 'B', adp_rank: 2 },
    ]
    expect([...players].sort(snakeSortComparator).map((p) => p.name)).toEqual(['A', 'B', 'C', 'N'])
  })

  it('snakeSortComparator ignores adp_ai (would mis-order on ties)', () => {
    // All tied on adp_ai=4 but distinct ranks — must order by rank.
    const players = [
      { name: 'third', adp_ai: 4, adp_rank: 3 },
      { name: 'first', adp_ai: 4, adp_rank: 1 },
      { name: 'second', adp_ai: 4, adp_rank: 2 },
    ]
    expect([...players].sort(snakeSortComparator).map((p) => p.name)).toEqual(['first', 'second', 'third'])
  })

  it('auctionSortComparator sorts by ceiling descending', () => {
    const players = [
      { name: 'lo', ai_bid_ceiling: 10 },
      { name: 'hi', ai_bid_ceiling: 80 },
      { name: 'mid', ai_bid_ceiling: 40 },
    ]
    expect([...players].sort(auctionSortComparator).map((p) => p.name)).toEqual(['hi', 'mid', 'lo'])
  })
})

describe('playerUtils — snake flag', () => {
  it('getSnakeFlag returns config for a known flag', () => {
    expect(getSnakeFlag(PLAYER)).toMatchObject({ label: 'VALUE', color: 'green' })
  })

  it('getSnakeFlag defaults to TARGET for unknown/missing', () => {
    expect(getSnakeFlag({}).label).toBe('TARGET')
  })

  it('getSnakeFlagClass returns Tailwind classes', () => {
    expect(getSnakeFlagClass(PLAYER)).toContain('emerald')
  })

  it('getSnakeFlagLabel returns the raw flag or null', () => {
    expect(getSnakeFlagLabel(PLAYER)).toBe('VALUE')
    expect(getSnakeFlagLabel({})).toBeNull()
  })
})

describe('playerUtils — recommendation shape', () => {
  it('getRecAdp prefers adp_rank, falls back to rounded adp_ai', () => {
    expect(getRecAdp({ adp_rank: 1, adp_ai: 4.2 })).toBe(1)
    expect(getRecAdp({ adp_ai: 4.6 })).toBe(5) // legacy nomination rec
    expect(getRecAdp({})).toBeNull()
  })

  it('getRecFpAdp reads adp_fp (engine field name)', () => {
    expect(getRecFpAdp({ adp_fp: 7 })).toBe(7)
    expect(getRecFpAdp({})).toBeNull()
  })

  it('getRecAdpDiff reads adp_diff', () => {
    expect(getRecAdpDiff({ adp_diff: -2 })).toBe(-2)
  })
})

describe('no component reads raw ADP fields directly', () => {
  it('only playerUtils / stores / tests touch the raw fields', () => {
    // Guard against the duplicated-field-selection regression: components must
    // import from playerUtils, never read player.adp_ai / .adp_rank etc.
    // Tolerate being run from frontend/ (CI) or the repo root.
    let root = 'src'
    try {
      readdirSync(root)
    } catch {
      root = 'frontend/src'
    }

    function walk(dir) {
      const out = []
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = join(dir, entry.name)
        if (entry.isDirectory()) out.push(...walk(full))
        else if (/\.(js|jsx)$/.test(entry.name)) out.push(full)
      }
      return out
    }

    const RAW = /\.(adp_ai|adp_rank|adp_fantasypros|adp_diff|ai_bid_ceiling|snake_flag)\b/
    const ALLOWED = /playerUtils|[/\\]stores[/\\]|[/\\]api[/\\]|[/\\]test[/\\]|\.test\.|\.spec\./
    const offenders = []
    for (const f of walk(root)) {
      if (ALLOWED.test(f)) continue
      const src = readFileSync(f, 'utf-8')
      src.split('\n').forEach((line, i) => {
        if (RAW.test(line)) offenders.push(`${f}:${i + 1}  ${line.trim()}`)
      })
    }
    expect(offenders).toEqual([])
  })
})
