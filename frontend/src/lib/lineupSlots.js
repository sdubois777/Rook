/**
 * Where a player is seated in their manager's lineup.
 *
 * The backend sends the slot the PLATFORM reports, normalized to the canonical slot
 * names ("QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX", "K", "DEF", "BENCH", "IR").
 * It is three-state: null means the platform did not tell us, which is the honest
 * answer for a platform whose roster shape has not been verified. Until this was
 * wired the value was null for every player on every real league, so every roster
 * rendered as entirely bench with no slot labels at all.
 */

/** Slots that are NOT a starting position, and the different reasons why. */
const NOT_STARTING = new Set([
  'BENCH',   // benched by the manager
  'IR',      // manager placed them on injured reserve — not playing
])

/**
 * Is this player actually starting?
 *
 * Returns false for an unknown slot. Treating unknown as starting would put a chip
 * on every player of a league whose slots we could not read, and treating it as
 * benched at least matches what the page showed before any of this was wired.
 *
 * The injured-reserve case is why this function exists rather than an inline
 * "not bench" test: a plain `slot !== 'BENCH'` check reads injured reserve as a
 * starting slot and renders an "IR" chip among the starters.
 */
export function isStartingSlot(slot) {
  return !!slot && !NOT_STARTING.has(slot)
}

/** Is the manager holding this player on injured reserve? */
export function isInjuredReserve(slot) {
  return slot === 'IR'
}
