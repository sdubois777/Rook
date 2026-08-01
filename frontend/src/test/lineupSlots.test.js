import { describe, it, expect } from 'vitest'
import { isStartingSlot, isInjuredReserve } from '../lib/lineupSlots'

/**
 * Where a player is seated, as the platform reports it.
 *
 * Two things are easy to get wrong here and both put a false statement on screen:
 * treating injured reserve as a starting slot, and treating "we could not read this
 * league's lineup" as "this player is benched".
 */
describe('isStartingSlot', () => {
  it('accepts real starting slots, including the multi-position ones', () => {
    for (const slot of ['QB', 'RB', 'WR', 'TE', 'K', 'DEF', 'FLEX', 'SUPER_FLEX']) {
      expect(isStartingSlot(slot)).toBe(true)
    }
  })

  it('does not treat injured reserve as a starting slot', () => {
    // A plain "is not BENCH" test reads IR as starting and puts an IR chip in the
    // lineup, which says the manager is starting a player they have shelved.
    expect(isStartingSlot('IR')).toBe(false)
  })

  it('does not treat the bench as a starting slot', () => {
    expect(isStartingSlot('BENCH')).toBe(false)
  })

  it('treats an unknown slot as not starting, and never throws on one', () => {
    // null is what arrives for a platform whose roster shape we could not read. It
    // means we do not know, and the page must not claim the player is starting.
    for (const slot of [null, undefined, '']) {
      expect(isStartingSlot(slot)).toBe(false)
    }
  })
})

describe('isInjuredReserve', () => {
  it('is true only for injured reserve', () => {
    expect(isInjuredReserve('IR')).toBe(true)
    for (const slot of ['BENCH', 'RB', 'FLEX', null, undefined, '']) {
      expect(isInjuredReserve(slot)).toBe(false)
    }
  })

  it('never overlaps with a starting slot', () => {
    for (const slot of ['QB', 'RB', 'WR', 'TE', 'K', 'DEF', 'FLEX', 'SUPER_FLEX',
                        'BENCH', 'IR', null, undefined]) {
      expect(isStartingSlot(slot) && isInjuredReserve(slot)).toBe(false)
    }
  })
})
