import {
  Activity,
  AlertTriangle,
  TrendingUp,
  Star,
  ArrowLeftRight,
} from 'lucide-react'
import { PlayerBadges } from './PlayerName'

// Keys are the ACTUAL signal_type values the beat-reporter agent emits
// (backend/agents/beat_reporter.py SIGNAL_TYPES) — anything else falls back to
// the neutral default below. (Earlier maps keyed off types the agent never
// produces, so every item rendered gray.)
const signalIcons = {
  injury_flag: AlertTriangle,
  practice_limited: Activity,
  depth_chart_change: TrendingUp,
  camp_standout: Star,
  transaction: ArrowLeftRight,
}

const signalColors = {
  injury_flag: 'text-red-400',
  practice_limited: 'text-amber-400',
  depth_chart_change: 'text-blue-400',
  camp_standout: 'text-emerald-400',
  transaction: 'text-purple-400',
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

          {/* Headline — always visible and WRAPS fully (never CSS-clipped). Links
              out to the source article when we have a permalink; plain text
              otherwise (legacy rows carry no url and degrade gracefully). Note:
              some feeds (e.g. ESPN) truncate their OWN titles with a trailing
              "…" — that's source data, so the link is the path to the full story. */}
          {signal.raw_text &&
            (signal.article_url ? (
              <a
                href={signal.article_url}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-1 block text-sm text-slate-200 hover:text-white hover:underline focus-visible:underline focus-visible:outline-none"
              >
                {signal.raw_text}
              </a>
            ) : (
              <p className="mt-1 text-sm text-slate-200">{signal.raw_text}</p>
            ))}

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
