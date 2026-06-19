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
  matchesPickName,
  getSnakeTargets,
  snakeUrgencyLabel,
  neededPositions,
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

describe('playerUtils — matchesPickName (abbreviated DOM names)', () => {
  it('matches an abbreviated first name against the full name', () => {
    expect(matchesPickName('Christian McCaffrey', 'C. MCCAFFREY')).toBe(true)
    expect(matchesPickName('George Pickens', 'G. PICKENS')).toBe(true)
    expect(matchesPickName('Josh Jacobs', 'J. JACOBS')).toBe(true)
  })

  it('matches an exact (full) name', () => {
    expect(matchesPickName('Bijan Robinson', 'Bijan Robinson')).toBe(true)
  })

  it('does not match a different last name', () => {
    expect(matchesPickName('Christian McCaffrey', 'C. KIRK')).toBe(false)
  })

  it('does not match a different first initial, same last name', () => {
    // "A. Brown" must NOT match "Marquise Brown".
    expect(matchesPickName('Marquise Brown', 'A. BROWN')).toBe(false)
  })

  it('drops apostrophes via normalizeName (JaMarr == Ja\'Marr)', () => {
    expect(matchesPickName("Ja'Marr Chase", "J. CHASE")).toBe(true)
  })

  it('returns false for empty inputs', () => {
    expect(matchesPickName('', 'C. MCCAFFREY')).toBe(false)
    expect(matchesPickName('Bijan Robinson', '')).toBe(false)
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

describe('getSnakeTargets — urgency-aware suggested targets', () => {
  it('filters to needed positions', () => {
    const roster = [{ position: 'QB' }] // QB filled
    const players = [
      { id: 'q', name: 'QB Guy', position: 'QB', adp_rank: 5 },
      { id: 'r', name: 'RB Guy', position: 'RB', adp_rank: 6 },
    ]
    const out = getSnakeTargets(roster, players, 1, 12).map((p) => p.name)
    expect(out).toContain('RB Guy')
    expect(out).not.toContain('QB Guy') // QB slot already filled
  })

  it('scores urgency high when FP ADP is near the current pick', () => {
    const near = { id: 'n', name: 'Near', position: 'RB', adp_rank: 30, adp_fantasypros: 25 }
    const far = { id: 'f', name: 'Far', position: 'RB', adp_rank: 31, adp_fantasypros: 200 }
    // Current pick 20: Near goes ~pick 25 (within a round) -> urgent first.
    const out = getSnakeTargets([], [far, near], 20, 12)
    expect(out[0].name).toBe('Near')
  })

  it('reduces urgency for a high adp_diff (market sleeping on them)', () => {
    const sleeper = { id: 's', name: 'Sleeper', position: 'RB', adp_rank: 30, adp_fantasypros: 25, adp_diff: 25 }
    const urgent = { id: 'u', name: 'Urgent', position: 'WR', adp_rank: 31, adp_fantasypros: 25, adp_diff: 0 }
    // Both gone-soon (+100), but sleeper -30 for diff>20 -> urgent ranks first.
    const out = getSnakeTargets([], [sleeper, urgent], 20, 12)
    expect(out[0].name).toBe('Urgent')
  })

  it('sorts by urgency, then by adp_rank', () => {
    // Equal urgency (both far, no diff) -> tiebreak adp_rank ascending.
    const a = { id: 'a', name: 'A', position: 'RB', adp_rank: 30, adp_fantasypros: 200 }
    const b = { id: 'b', name: 'B', position: 'WR', adp_rank: 10, adp_fantasypros: 200 }
    expect(getSnakeTargets([], [a, b], 1, 12).map((p) => p.name)).toEqual(['B', 'A'])
  })

  it('falls back to all available when no needed-position players exist', () => {
    const roster = [{ position: 'K' }] // K filled; needed = QB/RB/WR/TE/DEF/FLEX
    const players = [{ id: 'k', name: 'Kicker2', position: 'K', adp_rank: 140 }]
    // No needed-position candidate -> show best available anyway.
    expect(getSnakeTargets(roster, players, 1, 12).map((p) => p.name)).toContain('Kicker2')
  })

  it('returns [] for an empty board', () => {
    expect(getSnakeTargets([], [], 1, 12)).toEqual([])
  })
})

describe('snakeUrgencyLabel', () => {
  it('is Now within one round', () => {
    expect(snakeUrgencyLabel({ _picksUntilGone: 10 }, 12).text).toBe('Now')
  })
  it('is Soon within two rounds', () => {
    expect(snakeUrgencyLabel({ _picksUntilGone: 20 }, 12).text).toBe('Soon')
  })
  it('is Wait when far but adp_diff is high', () => {
    expect(snakeUrgencyLabel({ _picksUntilGone: 100, adp_diff: 15 }, 12).text).toBe('Wait')
  })
  it('is null when far and adp_diff is low', () => {
    expect(snakeUrgencyLabel({ _picksUntilGone: 100, adp_diff: 0 }, 12)).toBeNull()
  })
})

describe('getSnakeTargets lives in playerUtils (single source of truth)', () => {
  it('is exported from playerUtils', () => {
    expect(typeof getSnakeTargets).toBe('function')
    expect(typeof neededPositions).toBe('function')
  })

  it('is NOT defined inline in SuggestedTargets.jsx', () => {
    let src
    try {
      src = readFileSync('src/components/draft/SuggestedTargets.jsx', 'utf-8')
    } catch {
      src = readFileSync('frontend/src/components/draft/SuggestedTargets.jsx', 'utf-8')
    }
    expect(src).not.toMatch(/(export\s+)?function getSnakeTargets/)
    expect(src).toMatch(/import\s*\{[^}]*getSnakeTargets[^}]*\}\s*from\s*'\.\.\/\.\.\/utils\/playerUtils'/)
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
