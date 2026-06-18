import { useEffect } from 'react'
import { useDraftStore } from '../stores/draft'
import useDraftSocket from '../hooks/useDraftSocket'
import { fetchDraftboard } from '../api/draftboard'
import ErrorBoundary from '../components/ErrorBoundary'
import DraftSetup from '../components/draft/DraftSetup'
import RecommendationPanel from '../components/draft/RecommendationPanel'
import SuggestedTargets from '../components/draft/SuggestedTargets'
import NominationPanel from '../components/draft/NominationPanel'
import SnakePanel from '../components/draft/SnakePanel'
import AvailablePlayers from '../components/draft/AvailablePlayers'
import TeamRosterPanel from '../components/draft/TeamRosterPanel'
import { useLeague } from '../context/LeagueContext'

const WS_STATUS_LABEL = {
  connected: { text: 'Connected', color: 'bg-emerald-500' },
  reconnecting: { text: 'Reconnecting...', color: 'bg-amber-500' },
  disconnected: { text: 'Disconnected', color: 'bg-red-500' },
}

export default function DraftRoom() {
  const phase = useDraftStore((s) => s.phase)
  const wsStatus = useDraftStore((s) => s.wsStatus)
  const setAvailablePlayers = useDraftStore((s) => s.setAvailablePlayers)
  const { isSnake } = useLeague()

  // Connect WebSocket
  useDraftSocket()

  // Load the available-players list on mount, independent of draft engine
  // state — so the list is populated even before "Start Draft", and a failed
  // engine start can't leave it empty. setAvailablePlayers ignores empty results.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const board = await fetchDraftboard()
        const tiers = board?.tiers || {}
        const players = Object.values(tiers).flat()
        // Diagnostics: if this logs 0 players, the issue is the /draftboard
        // response (empty data / auth / wrong shape), not the UI wiring.
        console.debug(
          'DraftMind: draftboard loaded —',
          players.length,
          'players; tier keys:',
          Object.keys(tiers),
        )
        if (!cancelled) setAvailablePlayers(players)
      } catch (e) {
        console.error('DraftMind: failed to load draftboard:', e?.response?.status, e)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [setAvailablePlayers])

  if (phase === 'setup') {
    return (
      <div className="h-screen bg-[#0f1117] text-slate-200">
        <DraftSetup />
      </div>
    )
  }

  if (phase === 'ended') {
    return (
      <div className="h-screen bg-[#0f1117] text-slate-200 flex items-center justify-center">
        <div className="text-center">
          <h1 className="text-2xl font-semibold text-slate-100 mb-2">Draft Complete</h1>
          <p className="text-slate-500">Session ended. Return to the draft board to review.</p>
          <a
            href="/draftboard"
            className="inline-block mt-4 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-500 transition-colors"
          >
            View Draft Board
          </a>
        </div>
      </div>
    )
  }

  const statusInfo = WS_STATUS_LABEL[wsStatus] || WS_STATUS_LABEL.disconnected

  return (
    <div className="h-screen bg-[#0f1117] text-slate-200 flex flex-col overflow-hidden">
      {/* Status bar */}
      <div className="flex items-center justify-between px-4 py-1.5 bg-[#161822] border-b border-[#2d3148]">
        <span className="text-sm font-medium text-slate-300">Draft Room</span>
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${statusInfo.color}`} />
          <span className="text-xs text-slate-500">{statusInfo.text}</span>
        </div>
      </div>

      {/* 3-column layout — fills the viewport, only inner lists scroll */}
      <div className="flex-1 grid grid-cols-[30%_40%_30%] min-h-0">
        {/* LEFT: compact Recommendation card over Suggested Targets (scrolls) */}
        <div className="border-r border-[#2d3148] min-h-0 flex flex-col overflow-hidden">
          <div className="shrink-0 border-b border-[#2d3148]">
            <ErrorBoundary
              fallback={
                <div className="flex items-center justify-center p-4 text-red-400 text-sm">
                  Recommendation error
                </div>
              }
            >
              <RecommendationPanel />
            </ErrorBoundary>
          </div>
          <div className="flex-1 min-h-0 overflow-hidden">
            <SuggestedTargets />
          </div>
        </div>

        {/* CENTER: Nomination (fixed) over Available players (scrolls) */}
        <div className="border-r border-[#2d3148] min-h-0 flex flex-col overflow-hidden">
          <div className="h-[190px] shrink-0 border-b border-[#2d3148] overflow-hidden">
            {isSnake ? <SnakePanel /> : <NominationPanel />}
          </div>
          <div className="flex-1 min-h-0 overflow-hidden">
            <AvailablePlayers />
          </div>
        </div>

        {/* RIGHT: Team rosters */}
        <div className="min-h-0 overflow-hidden">
          <TeamRosterPanel />
        </div>
      </div>
    </div>
  )
}
