/**
 * Shared display constants — labels duplicated across pages live here.
 */

export const SKILL_POSITIONS = ['QB', 'RB', 'WR', 'TE']

// The draft-board / available-players position-filter set. K and DEF are now
// valued ($1 streamers) and appear in the lists, so the dropdown offers them too.
// Kept separate from SKILL_POSITIONS so the latter stays the true skill set.
export const DRAFT_FILTER_POSITIONS = ['QB', 'RB', 'WR', 'TE', 'K', 'DEF']

/** Filter-select options for positions; the all-entry label varies by page. */
export function buildPositionOptions(allLabel = 'All Positions') {
  return [
    { value: '', label: allLabel },
    ...DRAFT_FILTER_POSITIONS.map((p) => ({ value: p, label: p })),
  ]
}

export const SCORING_LABELS = { ppr: 'PPR', half_ppr: 'Half PPR', standard: 'Standard' }

export const DRAFT_LABELS = { auction: 'Auction', snake: 'Snake' }

// TIER labels, credit costs, prices, and packs are NOT defined here.
// They are fetched from GET /billing/pricing (backend/models/user.py is the
// single source of truth) via hooks/usePricing — hardcoding them re-creates
// the four-way pricing drift that hook exists to kill.
