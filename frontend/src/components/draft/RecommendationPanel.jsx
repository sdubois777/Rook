import { useDraftStore } from '../../stores/draft'
import { placeBid, passNomination } from '../../api/draft'
import PositionBadge from '../shared/PositionBadge'
import FlagBadge from '../shared/FlagBadge'

const ACTION_STYLES = {
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
  const myBudget = useDraftStore((s) => s.myBudget)
  const spendable = useDraftStore((s) => s.spendable)
  const rosterSlotsRemaining = useDraftStore((s) => s.rosterSlotsRemaining)

  if (!rec) {
    return (
      <div className="h-full flex items-center justify-center text-slate-500">
        <div className="text-center">
          <p className="text-lg">Waiting for nomination...</p>
          <p className="text-sm mt-1">AI recommendation will appear here</p>
        </div>
      </div>
    )
  }

  const style = ACTION_STYLES[rec.action] || ACTION_STYLES.pass
  const confStyle = CONFIDENCE_STYLES[rec.confidence] || CONFIDENCE_STYLES.low

  const handleBid = async () => {
    try {
      await placeBid(rec.bid_ceiling)
    } catch {
      // Bid failed — bridge may not be connected
    }
  }

  const handlePass = async () => {
    if (!confirm('Pass on this player?')) return
    try {
      await passNomination()
    } catch {
      // Pass failed
    }
  }

  const budget = rec.budget_summary || {
    your_remaining: myBudget,
    spendable_on_this_player: spendable,
    roster_slots_remaining: rosterSlotsRemaining,
  }

  return (
    <div className="h-full flex flex-col p-4 overflow-y-auto">
      {/* Action header */}
      <div className={`rounded-lg border p-4 mb-3 ${style.bg} ${style.border}`}>
        <div className="flex items-center justify-between mb-2">
          <span className={`text-3xl font-bold ${style.text}`}>
            {formatAction(rec.action, rec.bid_ceiling)}
          </span>
          <span className={`text-xs px-2 py-0.5 rounded-full ${confStyle}`}>
            {rec.confidence}
          </span>
        </div>
        <div className="flex items-center gap-2 mb-2">
          <PositionBadge position={rec.position} />
          <span className="text-lg font-medium text-slate-200">{rec.player_name}</span>
        </div>
        {rec.reasoning && (
          <p className="text-sm text-slate-400">{rec.reasoning}</p>
        )}
      </div>

      {/* Values */}
      <div className="grid grid-cols-3 gap-2 mb-3 text-center">
        <div className="bg-[#1c1f2e] rounded-lg p-2">
          <div className="text-xs text-slate-500">Ceiling</div>
          <div className="text-lg font-mono text-blue-400">${rec.bid_ceiling}</div>
        </div>
        <div className="bg-[#1c1f2e] rounded-lg p-2">
          <div className="text-xs text-slate-500">System</div>
          <div className="text-sm font-mono text-slate-300">${rec.system_value}</div>
        </div>
        <div className="bg-[#1c1f2e] rounded-lg p-2">
          <div className="text-xs text-slate-500">Market</div>
          <div className="text-sm font-mono text-slate-300">${rec.market_value}</div>
        </div>
      </div>

      {/* Flags */}
      {rec.active_flags?.length > 0 && (
        <div className="flex flex-wrap gap-1 mb-3">
          {rec.active_flags.map((f, i) => (
            <FlagBadge key={i} flagType={f.flag_type} compact />
          ))}
        </div>
      )}

      {/* Block value */}
      {rec.block_value > 0 && (
        <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded px-2 py-1 mb-3">
          Block value: ${rec.block_value.toFixed(0)}
          {rec.budget_allows_block ? ' — budget allows block' : ' — budget insufficient'}
        </div>
      )}

      {/* Opponent alerts */}
      {rec.opponent_alerts?.length > 0 && (
        <div className="space-y-1 mb-3">
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

      {/* Budget summary */}
      <div className="flex gap-3 text-xs text-slate-500 mb-3">
        <span>Budget: <span className="text-slate-300 font-mono">${budget.your_remaining}</span></span>
        <span>Spendable: <span className="text-slate-300 font-mono">${budget.spendable_on_this_player}</span></span>
        <span>Slots: <span className="text-slate-300 font-mono">{budget.roster_slots_remaining}</span></span>
      </div>

      {/* Elapsed */}
      {rec.elapsed_ms != null && (
        <div className="text-[10px] text-slate-600 mb-3">{rec.elapsed_ms}ms</div>
      )}

      {/* Action buttons — pushed to bottom */}
      <div className="mt-auto flex gap-2">
        {rec.action !== 'pass' && (
          <button
            onClick={handleBid}
            className="flex-1 py-2.5 bg-blue-600 text-white font-medium rounded-lg hover:bg-blue-500 transition-colors"
          >
            Bid ${rec.bid_ceiling}
          </button>
        )}
        <button
          onClick={handlePass}
          className={`py-2.5 px-4 bg-[#1c1f2e] text-slate-400 border border-[#2d3148] rounded-lg hover:bg-[#222539] transition-colors ${
            rec.action === 'pass' ? 'flex-1' : ''
          }`}
        >
          Pass
        </button>
      </div>
    </div>
  )
}
