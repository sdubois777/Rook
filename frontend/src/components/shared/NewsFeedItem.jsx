import {
  Activity,
  AlertTriangle,
  TrendingUp,
  UserPlus,
  UserMinus,
} from 'lucide-react'
import { PlayerBadges } from './PlayerName'

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

// Known RSS hosts → a clean publisher label. `source` is the FEED url (not the
// article), so it identifies the outlet but is not a per-article deep link.
const PUBLISHERS = {
  'espn.com': 'ESPN',
  'rotowire.com': 'RotoWire',
  'nfl.com': 'NFL.com',
  'profootballtalk.nbcsports.com': 'ProFootballTalk',
  'cbssports.com': 'CBS Sports',
  'yahoo.com': 'Yahoo Sports',
}

/** A readable outlet name from the feed URL, or null if unparseable. */
function sourceLabel(source) {
  if (!source) return null
  try {
    const host = new URL(source).hostname.replace(/^www\./, '')
    return PUBLISHERS[host] || host
  } catch {
    // Not a URL (older rows sometimes stored a bare name) — show it as-is.
    return source
  }
}

export default function NewsFeedItem({ signal, onPlayerClick }) {
  const Icon = signalIcons[signal.signal_type] || Activity
  const color = signalColors[signal.signal_type] || 'text-slate-400'
  const publisher = sourceLabel(signal.source)

  const time = signal.flagged_at
    ? new Date(signal.flagged_at).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
    : ''

  return (
    <div className="border-b border-border/50 px-4 py-3">
      <div className="flex items-start gap-3">
        <Icon size={16} className={`mt-0.5 shrink-0 ${color}`} />
        <div className="min-w-0 flex-1">
          {/* Meta row: type · confidence, with the timestamp pinned right */}
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className={`text-xs font-medium uppercase ${color}`}>
                {signal.signal_type.replace(/_/g, ' ')}
              </span>
              {signal.confidence && (
                <span className="text-[10px] text-slate-500">{signal.confidence}</span>
              )}
            </div>
            {time && <span className="shrink-0 text-[10px] text-slate-500">{time}</span>}
          </div>

          {/* Headline — the actual news, now always visible (was hidden behind a
              click). Clamped to two lines so long titles don't break the row. */}
          {signal.raw_text && (
            <p className="mt-1 line-clamp-2 text-sm text-slate-200">{signal.raw_text}</p>
          )}

          {/* Player + source attribution */}
          <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-sm text-slate-400">
            {signal.player_position && (
              <PlayerBadges
                position={signal.player_position}
                injuryStatus={signal.injury_status}
                variant="compact"
                className="align-middle"
              />
            )}
            {signal.player_name &&
              (signal.player_id && onPlayerClick ? (
                <button
                  onClick={() => onPlayerClick(signal.player_id)}
                  className="font-medium text-blue-400 hover:underline"
                >
                  {signal.player_name}
                </button>
              ) : (
                <span className="font-medium">{signal.player_name}</span>
              ))}
            {signal.player_team && (
              <span className="text-slate-500">({signal.player_team})</span>
            )}
            {publisher && (
              <span className="text-[11px] text-slate-500">
                {(signal.player_name || signal.player_position) && (
                  <span className="mr-1.5 text-slate-600">·</span>
                )}
                via {publisher}
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
