import { useDraftStore } from '../../stores/draft'
import PositionBadge from '../shared/PositionBadge'

const POSITION_SLOTS = {
  QB: 1,
  RB: 2,
  WR: 2,
  FLEX: 1,
  TE: 1,
  K: 1,
  DEF: 1,
  BN: 7,
}

function getBudgetColor(remaining, total) {
  const pct = remaining / total
  if (pct > 0.5) return 'bg-emerald-500'
  if (pct > 0.25) return 'bg-amber-500'
  return 'bg-red-500'
}

export default function MyRoster() {
  const myBudget = useDraftStore((s) => s.myBudget)
  const myRoster = useDraftStore((s) => s.myRoster)
  const spendable = useDraftStore((s) => s.spendable)
  const rosterSlotsRemaining = useDraftStore((s) => s.rosterSlotsRemaining)
  const teamsState = useDraftStore((s) => s.teamsState)
  const myTeamName = useDraftStore((s) => s.myTeamName)

  const totalBudget = 200
  // Prefer the live budget scraped from the draft room's team panel (keyed by
  // display name) — it updates on every pick, whereas myBudget only moves when
  // we win a player or a recommendation carries a budget_summary.
  const liveBudget =
    myTeamName && teamsState?.[myTeamName]?.budget != null
      ? teamsState[myTeamName].budget
      : undefined
  const displayBudget = liveBudget ?? myBudget
  const spent = totalBudget - displayBudget
  const budgetPct = Math.max(0, Math.min(100, (displayBudget / totalBudget) * 100))

  // Group roster by position
  const grouped = {}
  for (const pick of myRoster) {
    const pos = pick.position || 'BN'
    if (!grouped[pos]) grouped[pos] = []
    grouped[pos].push(pick)
  }

  return (
    <div className="h-full flex flex-col p-4 overflow-y-auto">
      <h3 className="text-sm font-medium text-slate-400 uppercase tracking-wider mb-3">
        My Roster
      </h3>

      {/* Budget bar */}
      <div className="bg-[#1c1f2e] rounded-lg p-3 mb-3">
        <div className="flex justify-between text-sm mb-2">
          <span className="text-slate-300">
            Budget: <span className="font-mono font-medium">${displayBudget}</span>
          </span>
          <span className="text-slate-500">
            Spent: <span className="font-mono">${spent}</span>
          </span>
        </div>
        <div className="h-2 bg-[#0f1117] rounded-full overflow-hidden mb-2">
          <div
            className={`h-full rounded-full transition-all ${getBudgetColor(displayBudget, totalBudget)}`}
            style={{ width: `${budgetPct}%` }}
          />
        </div>
        <div className="flex gap-4 text-xs text-slate-500">
          <span>
            Spendable: <span className="text-slate-300 font-mono">${spendable}</span>
          </span>
          <span>
            Slots: <span className="text-slate-300 font-mono">{rosterSlotsRemaining}</span>
          </span>
        </div>
      </div>

      {/* Roster grid */}
      <div className="space-y-1 flex-1">
        {Object.entries(POSITION_SLOTS).map(([pos, count]) => {
          const fills = grouped[pos] || []
          const slots = []
          for (let i = 0; i < count; i++) {
            const pick = fills[i]
            slots.push(
              <div
                key={`${pos}-${i}`}
                className={`flex items-center gap-2 px-3 py-1.5 rounded ${
                  pick
                    ? 'bg-[#1c1f2e]'
                    : 'border border-dashed border-[#2d3148]'
                }`}
              >
                <span className="text-[10px] text-slate-600 w-6 font-mono">{pos}</span>
                {pick ? (
                  <>
                    <PositionBadge position={pick.position} />
                    <span className="text-sm text-slate-300 flex-1">{pick.player_name}</span>
                    <span className="text-xs font-mono text-slate-500">${pick.price}</span>
                  </>
                ) : (
                  <span className="text-xs text-slate-700 italic">Empty</span>
                )}
              </div>
            )
          }
          return slots
        })}
      </div>
    </div>
  )
}
