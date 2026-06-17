import { useMemo } from 'react'
import { useDraftStore } from '../../stores/draft'
import { useLeague } from '../../context/LeagueContext'
import PositionBadge from '../shared/PositionBadge'

// Starter lineup this league fields (bench excluded — bench never drives a
// "need"). FLEX accepts RB/WR/TE.
export const STARTER_SLOTS = { QB: 1, RB: 2, WR: 2, TE: 1, FLEX: 1, K: 1, DEF: 1 }

// Keep $1 in reserve for every roster slot still to fill after this one, so a
// suggestion never recommends spending money you legally must keep.
function spendableFor(myBudget, rosterSlotsRemaining) {
  const slotsLeft = rosterSlotsRemaining || 1
  return Math.max(myBudget - (slotsLeft - 1), 0)
}

// Which starter positions does this roster still need? FLEX opens RB/WR/TE.
export function neededPositions(myRoster) {
  const filledSlots = {}
  for (const pick of myRoster) {
    const pos = pick.position || 'BN'
    filledSlots[pos] = (filledSlots[pos] || 0) + 1
  }

  const needed = new Set()
  for (const [pos, count] of Object.entries(STARTER_SLOTS)) {
    if ((filledSlots[pos] || 0) < count) needed.add(pos)
  }
  if ((filledSlots['FLEX'] || 0) < 1) {
    needed.add('RB')
    needed.add('WR')
    needed.add('TE')
  }
  return needed
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
      (p) => needed.has(p.position) && (p.ai_bid_ceiling || 0) <= spendable
    )
    .sort((a, b) => (b.ai_bid_ceiling || 0) - (a.ai_bid_ceiling || 0))
    .slice(0, 8)
}

// Snake equivalent: needed-position players sorted by AI ADP ascending (lower =
// earlier pick = take sooner). Budget is irrelevant in snake; players without an
// ADP yet sort last. Top 8.
export function getSnakeTargets(myRoster, availablePlayers) {
  const needed = neededPositions(myRoster)
  return availablePlayers
    .filter((p) => needed.has(p.position))
    .slice()
    .sort((a, b) => (a.adp_ai ?? Infinity) - (b.adp_ai ?? Infinity))
    .slice(0, 8)
}

// Fallback when every starter slot is filled: best value still on the board
// within budget, regardless of position.
function getValuePicks(availablePlayers, myBudget, rosterSlotsRemaining) {
  const spendable = spendableFor(myBudget, rosterSlotsRemaining)
  return availablePlayers
    .filter((p) => (p.ai_bid_ceiling || 0) <= spendable)
    .sort((a, b) => (b.ai_bid_ceiling || 0) - (a.ai_bid_ceiling || 0))
    .slice(0, 8)
}

function TargetRow({ player, isSnake }) {
  const ceiling = player.ai_bid_ceiling || 0
  const market = player.market_value || 0
  const isValue = ceiling - market > 5
  return (
    <div className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-[#222539] transition-colors">
      <PositionBadge position={player.position} />
      <span className="text-sm text-slate-300 flex-1 truncate">{player.name}</span>
      {isSnake ? (
        <span className="text-sm font-mono text-blue-400">
          ADP {player.adp_ai != null ? player.adp_ai.toFixed(1) : '--'}
        </span>
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
  const { isSnake } = useLeague()

  const { rows, allFilled } = useMemo(() => {
    if (isSnake) {
      // Snake: ADP-ranked needed positions (no budget constraint).
      return { rows: getSnakeTargets(myRoster, availablePlayers), allFilled: false }
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
  }, [myRoster, availablePlayers, myBudget, rosterSlotsRemaining, isSnake])

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
            <TargetRow key={p.id || p.yahoo_player_id || p.name} player={p} isSnake={isSnake} />
          ))
        )}
      </div>
    </div>
  )
}
