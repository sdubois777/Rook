import { useState } from 'react'
import {
  Activity,
  AlertTriangle,
  TrendingUp,
  TrendingDown,
  UserPlus,
  UserMinus,
  ChevronDown,
  ChevronUp,
} from 'lucide-react'

const signalIcons = {
  injury_update: AlertTriangle,
  practice_status: Activity,
  depth_chart_move: TrendingUp,
  trade: UserPlus,
  release: UserMinus,
  suspension: AlertTriangle,
  contract: TrendingUp,
  coaching_change: Activity,
}

const signalColors = {
  injury_update: 'text-red-400',
  practice_status: 'text-amber-400',
  depth_chart_move: 'text-blue-400',
  trade: 'text-emerald-400',
  release: 'text-orange-400',
  suspension: 'text-red-400',
  contract: 'text-teal-400',
  coaching_change: 'text-purple-400',
}

export default function NewsFeedItem({ signal, onPlayerClick }) {
  const [expanded, setExpanded] = useState(false)
  const Icon = signalIcons[signal.signal_type] || Activity
  const color = signalColors[signal.signal_type] || 'text-slate-400'

  const time = signal.flagged_at
    ? new Date(signal.flagged_at).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
    : ''

  return (
    <div className="border-b border-border/50 py-3 px-4">
      <div
        className="flex items-start gap-3 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <Icon size={16} className={`mt-0.5 shrink-0 ${color}`} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className={`text-xs font-medium uppercase ${color}`}>
              {signal.signal_type.replace(/_/g, ' ')}
            </span>
            {signal.confidence && (
              <span className="text-[10px] text-slate-500">
                {signal.confidence}
              </span>
            )}
          </div>
          <div className="text-sm text-slate-300 mt-0.5">
            {signal.player_name && (
              signal.player_id && onPlayerClick ? (
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    onPlayerClick(signal.player_id)
                  }}
                  className="font-medium text-blue-400 hover:underline"
                >
                  {signal.player_name}
                </button>
              ) : (
                <span className="font-medium">{signal.player_name}</span>
              )
            )}
            {signal.player_team && (
              <span className="text-slate-500 ml-1">({signal.player_team})</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-[10px] text-slate-500">{time}</span>
          {signal.raw_text && (
            expanded ? <ChevronUp size={14} className="text-slate-500" /> : <ChevronDown size={14} className="text-slate-500" />
          )}
        </div>
      </div>
      {expanded && signal.raw_text && (
        <div className="mt-2 ml-7 text-xs text-slate-400 leading-relaxed">
          {signal.raw_text}
        </div>
      )}
    </div>
  )
}
