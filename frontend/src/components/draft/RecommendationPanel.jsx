import { useDraftStore } from '../../stores/draft'
import { useLeague } from '../../context/LeagueContext'
import PositionBadge from '../shared/PositionBadge'
import FlagBadge from '../shared/FlagBadge'

// Keys are LOWERCASE — the engine sends action as buy|bid_to|block|pass.
export const ACTION_STYLES = {
  buy: { bg: 'bg-emerald-500/20', text: 'text-emerald-400', border: 'border-emerald-500/30' },
  bid_to: { bg: 'bg-blue-500/20', text: 'text-blue-400', border: 'border-blue-500/30' },
  block: { bg: 'bg-red-500/20', text: 'text-red-400', border: 'border-red-500/30' },
  pass: { bg: 'bg-slate-500/20', text: 'text-slate-400', border: 'border-slate-500/30' },
}

const CONFIDENCE_STYLES = {
  high: 'text-emerald-400 bg-emerald-500/15',
  medium: 'text-amber-400 bg-amber-500/15',
  low: 'text-red-400 bg-red-500/15',
}

function formatAction(action, ceiling) {
  switch (action) {
    case 'buy': return 'BUY'
    case 'bid_to': return `BID TO $${ceiling}`
    case 'block': return 'BLOCK'
    case 'pass': return 'PASS'
    default: return action?.toUpperCase() || ''
  }
}

export default function RecommendationPanel() {
  const rec = useDraftStore((s) => s.recommendation)
  const currentNomination = useDraftStore((s) => s.currentNomination)
  const myBudget = useDraftStore((s) => s.myBudget)
  const spendable = useDraftStore((s) => s.spendable)
  const rosterSlotsRemaining = useDraftStore((s) => s.rosterSlotsRemaining)
  const { isSnake } = useLeague()

  if (!rec) {
    // A nominee is on the block but the engine hasn't returned yet.
    if (currentNomination?.playerName) {
      return (
        <div className="p-4 text-slate-500">
          <p className="text-base text-slate-300">Analyzing...</p>
          <p className="text-sm mt-0.5">{currentNomination.playerName}</p>
        </div>
      )
    }
    return (
      <div className="p-4 text-slate-500">
        <p className="text-base">Waiting for nomination...</p>
        <p className="text-sm mt-0.5">AI recommendation will appear here</p>
      </div>
    )
  }

  const style = ACTION_STYLES[rec.action] || ACTION_STYLES.pass
  const confStyle = CONFIDENCE_STYLES[rec.confidence] || CONFIDENCE_STYLES.low

  const budget = rec.budget_summary || {
    your_remaining: myBudget,
    spendable_on_this_player: spendable,
    roster_slots_remaining: rosterSlotsRemaining,
  }

  // Compact card — the rest of the left column is the Suggested Targets list.
  return (
    <div className="flex flex-col p-3">
      <div className={`rounded-lg border p-3 ${style.bg} ${style.border}`}>
        <div className="flex items-center justify-between mb-1.5">
          <span className={`text-2xl font-bold ${style.text}`}>
            {isSnake
              ? `TARGET PICK ${rec.adp_ai != null ? Math.round(rec.adp_ai) : '--'}`
              : formatAction(rec.action, rec.bid_ceiling)}
          </span>
          <span className={`text-xs px-2 py-0.5 rounded-full ${confStyle}`}>
            {rec.confidence}
          </span>
        </div>
        <div className="flex items-center gap-2 mb-1.5">
          <PositionBadge position={rec.position} />
          <span className="text-base font-medium text-slate-200 truncate">
            {rec.player_name}
          </span>
        </div>
        {rec.reasoning && <p className="text-sm text-slate-400">{rec.reasoning}</p>}

        {/* Flags (compact) */}
        {rec.active_flags?.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-2">
            {rec.active_flags.map((f, i) => (
              <FlagBadge key={i} flagType={f.flag_type} compact />
            ))}
          </div>
        )}

        {/* Opponent alerts (compact, only when present) */}
        {rec.opponent_alerts?.length > 0 && (
          <div className="space-y-1 mt-2">
            {rec.opponent_alerts.map((alert, i) => (
              <div
                key={i}
                className="text-xs text-amber-400 bg-amber-500/10 border border-amber-500/20 rounded px-2 py-1"
              >
                {alert}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Values + budget — single compact lines (auction $ vs snake ADP) */}
      {isSnake ? (
        <div className="flex gap-3 text-xs text-slate-500 mt-2 font-mono">
          <span>AI ADP <span className="text-blue-400">{rec.adp_ai ?? '--'}</span></span>
          <span>FP ADP <span className="text-slate-300">{rec.adp_fp ?? '--'}</span></span>
          <span>Diff <span className="text-slate-300">{rec.adp_diff ?? '--'}</span></span>
        </div>
      ) : (
        <div className="flex gap-3 text-xs text-slate-500 mt-2 font-mono">
          <span>Ceil <span className="text-blue-400">${rec.bid_ceiling}</span></span>
          <span>Sys <span className="text-slate-300">${rec.system_value}</span></span>
          <span>Mkt <span className="text-slate-300">${rec.market_value}</span></span>
        </div>
      )}
      {!isSnake && (
        <div className="flex gap-3 text-xs text-slate-500 mt-1">
          <span>Budget <span className="text-slate-300 font-mono">${budget.your_remaining}</span></span>
          <span>Spendable <span className="text-slate-300 font-mono">${budget.spendable_on_this_player}</span></span>
        </div>
      )}
    </div>
  )
}
