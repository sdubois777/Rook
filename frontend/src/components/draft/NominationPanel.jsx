import { useDraftStore } from '../../stores/draft'
import PositionBadge from '../shared/PositionBadge'

// Where the live bid sits relative to our own system value.
function valueIndicator(bid, systemValue) {
  if (bid == null || systemValue == null) return null
  const gap = bid - systemValue
  if (gap <= -3) return { label: 'Undervalued', cls: 'text-emerald-400' }
  if (gap >= 3) return { label: 'Overvalued', cls: 'text-red-400' }
  return { label: 'Aligned', cls: 'text-slate-400' }
}

export default function NominationPanel() {
  const rec = useDraftStore((s) => s.recommendation)
  const currentBid = useDraftStore((s) => s.currentBid)
  const currentNomination = useDraftStore((s) => s.currentNomination)

  const nom = currentNomination
  const playerName =
    nom?.playerName || rec?.player_name || currentBid?.player_name || 'Unknown'
  const bidAmount = nom?.currentBid ?? currentBid?.current_bid
  const clock = nom?.clock
  const clockDanger = nom?.secondsRemaining != null && nom.secondsRemaining < 10
  const hasNomination = nom || rec || currentBid

  const systemValue = rec?.system_value ?? null
  const marketValue = rec?.market_value ?? null
  const indicator = valueIndicator(bidAmount, systemValue)

  if (!hasNomination) {
    return (
      <div className="h-full flex flex-col p-3">
        <h3 className="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">
          Current Nomination
        </h3>
        <div className="flex-1 flex items-center justify-center text-slate-600">
          <p>Waiting for nomination...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col p-3">
      <h3 className="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">
        Current Nomination
      </h3>

      {/* Nominee */}
      <div className="flex items-center gap-2 mb-2">
        <PositionBadge position={rec?.position} />
        <span className="text-lg font-bold text-slate-100 truncate">{playerName}</span>
        {nom?.posTeam && <span className="text-xs text-slate-500">{nom.posTeam}</span>}
      </div>

      {/* Current bid + clock */}
      <div className="bg-[#1c1f2e] rounded-lg px-3 py-2 mb-2 flex items-center justify-between">
        <div>
          <div className="text-xs text-slate-500 mb-0.5">Current Bid</div>
          <div className="text-2xl font-mono font-bold text-amber-400">
            ${bidAmount ?? '--'}
          </div>
        </div>
        {clock && (
          <div className="text-right">
            <div className="text-xs text-slate-500 mb-0.5">Remaining</div>
            <div
              className={`text-2xl font-mono font-bold ${
                clockDanger ? 'text-red-500' : 'text-slate-300'
              }`}
            >
              {clock}
            </div>
          </div>
        )}
      </div>

      {/* System / Market + value indicator */}
      {(systemValue != null || marketValue != null) && (
        <div className="flex items-center gap-4 text-sm">
          <span className="text-slate-500">
            System: <span className="font-mono text-slate-300">${systemValue ?? '--'}</span>
          </span>
          <span className="text-slate-500">
            Market: <span className="font-mono text-slate-300">${marketValue ?? '--'}</span>
          </span>
          {indicator && (
            <span className={`ml-auto text-xs font-medium ${indicator.cls}`}>
              {indicator.label}
            </span>
          )}
        </div>
      )}
    </div>
  )
}
