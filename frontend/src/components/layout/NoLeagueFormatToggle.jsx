import { useLeague } from '../../context/LeagueContext'
import { useUIStore } from '../../stores/ui'
import { SCORING_LABELS, DRAFT_LABELS } from '../../lib/constants'

const DRAFT_TYPES = ['snake', 'auction']
const SCORINGS = ['ppr', 'half_ppr', 'standard']

// Short chip for the collapsed desktop rail, e.g. "SNK · PPR".
function chipLabel(draftType, scoringFormat) {
  const draft = draftType === 'auction' ? 'AUC' : 'SNK'
  const scoring = { ppr: 'PPR', half_ppr: 'HALF', standard: 'STD' }[scoringFormat] || 'PPR'
  return `${draft} · ${scoring}`
}

/**
 * Format picker for users with NO synced league.
 *
 * Rendered by LeagueSelector in place of its `return null` — so exactly one of the two
 * always renders, and the mutual exclusion is structural rather than a condition that
 * could disagree with itself. (LeagueSelector owns the league fetch and auto-selects
 * list[0] inside the .then, so there is a frame where leagues exist but selectedLeague is
 * still null; a sibling gated on `!selectedLeague` would flash both controls.)
 *
 * A synced league OUTRANKS this choice in LeagueContext's precedence chain, which is why
 * it is correct for this control to simply not exist once one is connected.
 */
export default function NoLeagueFormatToggle() {
  const collapsed = useUIStore((s) => s.sidebarCollapsed)
  const { draftType, scoringFormat, setFormatOverride } = useLeague()

  // min-h-11 for the ≥44px touch target on mobile, reverted at lg so desktop density
  // is unchanged (frontend/CLAUDE.md).
  const selectClass =
    'w-full bg-surface-2 text-slate-200 border border-border rounded px-2 py-1.5 text-xs ' +
    'min-h-11 lg:min-h-0'

  const full = (
    <div className="px-3 py-2 border-b border-border space-y-2">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">No league synced</div>
      <select
        aria-label="Draft type"
        value={draftType}
        onChange={(e) => setFormatOverride({ draft_type: e.target.value })}
        className={selectClass}
      >
        {DRAFT_TYPES.map((t) => (
          <option key={t} value={t}>{DRAFT_LABELS[t]}</option>
        ))}
      </select>
      <select
        aria-label="Scoring format"
        value={scoringFormat}
        onChange={(e) => setFormatOverride({ scoring: e.target.value })}
        className={selectClass}
      >
        {SCORINGS.map((s) => (
          <option key={s} value={s}>{SCORING_LABELS[s]}</option>
        ))}
      </select>
    </div>
  )

  // Desktop-collapsed rail: initials chip only at lg; the mobile drawer is full-width so
  // it still gets the real controls. Mirrors LeagueSelector's own collapsed handling.
  if (collapsed) {
    return (
      <>
        <div className="hidden lg:flex px-2 py-2 border-b border-border justify-center">
          <span
            title="No league synced — using default format"
            className="text-[10px] font-semibold text-slate-400 bg-surface-2 rounded px-1.5 py-1"
          >
            {chipLabel(draftType, scoringFormat)}
          </span>
        </div>
        <div className="lg:hidden">{full}</div>
      </>
    )
  }

  return full
}
