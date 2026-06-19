import { useMemo } from 'react'
import { useDraftStore } from '../../stores/draft'
import { useLeague } from '../../context/LeagueContext'
import PositionBadge from '../shared/PositionBadge'
import {
  getBidCeiling,
  formatAdp,
  neededPositions,
  getSnakeTargets,
  snakeUrgencyLabel,
  auctionSortComparator,
} from '../../utils/playerUtils'

// Keep $1 in reserve for every roster slot still to fill after this one, so a
// suggestion never recommends spending money you legally must keep.
function spendableFor(myBudget, rosterSlotsRemaining) {
  const slotsLeft = rosterSlotsRemaining || 1
  return Math.max(myBudget - (slotsLeft - 1), 0)
}

// Best available players for the positions still needed, within budget,
// highest ceiling first, capped at 8.
export function getSuggestedTargets(
  myRoster,
  availablePlayers,
  myBudget,
  rosterSlotsRemaining
) {
  const needed = neededPositions(myRoster)
  const spendable = spendableFor(myBudget, rosterSlotsRemaining)

  return availablePlayers
    .filter(
      (p) => needed.has(p.position) && (getBidCeiling(p) || 0) <= spendable
    )
    .sort(auctionSortComparator)
    .slice(0, 8)
}

// Fallback when every starter slot is filled: best value still on the board
// within budget, regardless of position.
function getValuePicks(availablePlayers, myBudget, rosterSlotsRemaining) {
  const spendable = spendableFor(myBudget, rosterSlotsRemaining)
  return availablePlayers
    .filter((p) => (getBidCeiling(p) || 0) <= spendable)
    .sort(auctionSortComparator)
    .slice(0, 8)
}

function TargetRow({ player, isSnake, totalTeams }) {
  const ceiling = getBidCeiling(player) || 0
  const market = player.market_value || 0
  const isValue = ceiling - market > 5
  const urgency = isSnake ? snakeUrgencyLabel(player, totalTeams) : null
  return (
    <div className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-[#222539] transition-colors">
      <PositionBadge position={player.position} />
      <span className="text-sm text-slate-300 flex-1 truncate">{player.name}</span>
      {isSnake ? (
        <>
          <span className="text-sm font-mono text-blue-400">ADP {formatAdp(player)}</span>
          {urgency && (
            <span className={`text-[11px] font-medium ${urgency.className}`}>
              {urgency.text}
            </span>
          )}
        </>
      ) : (
        <>
          <span className="text-sm font-mono text-blue-400">${Math.round(ceiling)}</span>
          {isValue && (
            <span className="text-emerald-400 text-xs" title="Ceiling exceeds market by $5+">
              ↑
            </span>
          )}
        </>
      )}
    </div>
  )
}

export default function SuggestedTargets() {
  const myRoster = useDraftStore((s) => s.myRoster)
  const availablePlayers = useDraftStore((s) => s.availablePlayers)
  const myBudget = useDraftStore((s) => s.myBudget)
  const rosterSlotsRemaining = useDraftStore((s) => s.rosterSlotsRemaining)
  const currentPick = useDraftStore((s) => s.currentPick)
  const { isSnake, selectedLeague } = useLeague()
  const totalTeams = selectedLeague?.team_count || 12

  const { rows, allFilled } = useMemo(() => {
    if (isSnake) {
      // Snake: urgency-ranked needed positions (will they be gone by your next
      // pick?), using the current pick + team count. No budget constraint.
      return {
        rows: getSnakeTargets(myRoster, availablePlayers, currentPick, totalTeams),
        allFilled: false,
      }
    }
    const allFilled = neededPositions(myRoster).size === 0
    const rows = allFilled
      ? getValuePicks(availablePlayers, myBudget, rosterSlotsRemaining)
      : getSuggestedTargets(
          myRoster,
          availablePlayers,
          myBudget,
          rosterSlotsRemaining
        )
    return { rows, allFilled }
  }, [myRoster, availablePlayers, myBudget, rosterSlotsRemaining, isSnake, currentPick, totalTeams])

  return (
    <div className="h-full flex flex-col p-3">
      <h3 className="text-xs font-medium text-slate-500 uppercase tracking-wider">
        Suggested Targets
      </h3>
      <p className="text-[11px] text-slate-600 mb-2">Based on roster needs + value</p>

      {allFilled && (
        <p className="text-xs text-amber-400 mb-2">
          All starter positions filled — targeting value picks
        </p>
      )}

      <div className="flex-1 overflow-y-auto -mx-1 px-1">
        {rows.length === 0 ? (
          <div className="text-slate-600 text-sm p-3 text-center">
            No targets available
          </div>
        ) : (
          rows.map((p) => (
            <TargetRow
              key={p.id || p.yahoo_player_id || p.name}
              player={p}
              isSnake={isSnake}
              totalTeams={totalTeams}
            />
          ))
        )}
      </div>
    </div>
  )
}
