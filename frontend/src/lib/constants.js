/**
 * Shared display constants — labels duplicated across pages live here.
 */

export const SKILL_POSITIONS = ['QB', 'RB', 'WR', 'TE']

/** Filter-select options for positions; the all-entry label varies by page. */
export function buildPositionOptions(allLabel = 'All Positions') {
  return [
    { value: '', label: allLabel },
    ...SKILL_POSITIONS.map((p) => ({ value: p, label: p })),
  ]
}

export const SCORING_LABELS = { ppr: 'PPR', half_ppr: 'Half PPR', standard: 'Standard' }

export const DRAFT_LABELS = { auction: 'Auction', snake: 'Snake' }

export const TIER_LABELS = {
  intro: 'Intro — $5/mo',
  standard: 'Standard — $9/mo',
  pro: 'Pro — $18/mo',
}

// Mirrors CREDIT_COSTS in backend/models/user.py (server is the source of truth;
// this is display-only — it labels how many credits an action will spend).
export const CREDIT_COSTS = {
  trade_analysis: 10,
  trade_finder: 20,
  waiver_wire: 8,
}
