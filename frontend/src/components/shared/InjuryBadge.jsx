/**
 * InjuryBadge — shared live injury-status tag shown beside a player's position badge.
 *
 * Reads the canonical code from the API (backend/utils/injury_status.py):
 *   "Q" questionable · "D" doubtful · "O" out · "IR" injured reserve · null = healthy.
 * Healthy players render NOTHING (no badge). Standard fantasy colors on the dark theme:
 * Q amber, D orange, O/IR red. Display-only — no value/metered path.
 */
const INJURY_CLS = {
  Q: 'bg-amber-500/15 text-amber-300',
  D: 'bg-orange-500/15 text-orange-300',
  O: 'bg-red-500/15 text-red-300',
  IR: 'bg-red-500/20 text-red-200',
}

const INJURY_TITLE = {
  Q: 'Questionable',
  D: 'Doubtful',
  O: 'Out',
  IR: 'Injured Reserve',
}

export default function InjuryBadge({ status }) {
  if (!status || !INJURY_CLS[status]) return null
  return (
    <span
      title={INJURY_TITLE[status]}
      className={`rounded px-1 py-0.5 text-[10px] font-bold leading-none ${INJURY_CLS[status]}`}
    >
      {status}
    </span>
  )
}
