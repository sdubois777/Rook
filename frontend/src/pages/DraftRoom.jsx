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
  const rehydrate = useDraftStore((s) => s.rehydrate)
  const endDraft = useDraftStore((s) => s.endDraft)
  const { isSnake } = useLeague()

  // Connect WebSocket
  useDraftSocket()

  // Deliberate, confirmed end of the draft (cannot be resumed). window.confirm is
  // modal and timer-free — no ambiguity on an irreversible mid-auction action.
  const handleEndDraft = () => {
    if (window.confirm("End this draft? You won't be able to resume it.")) {
      endDraft().catch((e) => console.error('Rook: end draft failed:', e))
    }
  }

  // On mount: if a backend draft session is active (e.g. a PAGE REFRESH reset the
  // in-memory store), rehydrate the full view from the server — rosters, budgets,
  // opponents, the drafted-filtered available list, AND the current suggested
  // pick. If there's no active draft, fall through to loading the raw draftboard
  // pool for the setup/browse view. (Skip when already 'live' from in-app nav, so
  // we don't disturb a healthy in-memory session.)
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      if (useDraftStore.getState().phase === 'live') return

      let hydrated = false
      try {
        hydrated = await rehydrate()
      } catch (e) {
        console.error('Rook: draft rehydrate failed:', e?.response?.status, e)
      }
      if (cancelled || hydrated) return // rehydrate set the filtered available list

      // No active draft → load the raw pool so setup/browse has players.
      try {
        const board = await fetchDraftboard()
        const players = Object.values(board?.tiers || {}).flat()
        if (!cancelled) setAvailablePlayers(players)
      } catch (e) {
        console.error('Rook: failed to load draftboard:', e?.response?.status, e)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [rehydrate, setAvailablePlayers])

  if (phase === 'setup') {
    return (
      <div className="h-screen bg-surface-0 text-slate-200">
        <DraftSetup />
      </div>
    )
  }

  if (phase === 'ended') {
    return (
      <div className="h-screen bg-surface-0 text-slate-200 flex items-center justify-center">
        <div className="text-center">
          <h1 className="text-2xl font-semibold text-slate-100 mb-2">Draft Complete</h1>
          <p className="text-slate-500">Session ended. Return to the draft board to review.</p>
          <a
            href="/draftboard"
            className="inline-block mt-4 px-4 py-2 bg-brand text-white rounded-lg hover:bg-brand-hover transition-colors"
          >
            View Draft Board
          </a>
        </div>
      </div>
    )
  }

  const statusInfo = WS_STATUS_LABEL[wsStatus] || WS_STATUS_LABEL.disconnected

  return (
    <div className="h-screen bg-surface-0 text-slate-200 flex flex-col overflow-hidden">
      {/* Status bar */}
      <div className="flex items-center justify-between px-4 py-1.5 bg-surface-1 border-b border-border">
        <span className="text-sm font-medium text-slate-300">Draft Room</span>
        <div className="flex items-center gap-3">
          <span className={`w-2 h-2 rounded-full ${statusInfo.color}`} />
          <span className="text-xs text-slate-500">{statusInfo.text}</span>
          {/* End Draft — the deliberate, irreversible "I'm done" signal that marks
              the backend session inactive so re-entering shows the board, not this
              finished draft. window.confirm (modal, no timer) guards an action
              that mid-auction would be catastrophic. */}
          <button
            onClick={handleEndDraft}
            className="text-xs text-slate-500 hover:text-red-400 border border-border hover:border-red-500/40 rounded px-2 py-0.5 transition-colors"
          >
            End Draft
          </button>
        </div>
      </div>

      {/* 3-column layout on desktop (fills the viewport, only inner lists
          scroll). On mobile the three zones stack and the whole area scrolls —
          live drafting is a desktop-extension flow, so this is "usable", not
          optimized. */}
      <div className="flex-1 grid grid-cols-1 lg:grid-cols-[30%_40%_30%] min-h-0 overflow-y-auto lg:overflow-hidden">
        {/* LEFT: compact Recommendation card over Suggested Targets (scrolls) */}
        <div className="border-b lg:border-b-0 lg:border-r border-border min-h-[70vh] lg:min-h-0 flex flex-col overflow-hidden">
          <div className="shrink-0 border-b border-border">
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
        <div className="border-b lg:border-b-0 lg:border-r border-border min-h-[70vh] lg:min-h-0 flex flex-col overflow-hidden">
          <div className="h-[190px] shrink-0 border-b border-border overflow-hidden">
            {isSnake ? <SnakePanel /> : <NominationPanel />}
          </div>
          <div className="flex-1 min-h-0 overflow-hidden">
            <AvailablePlayers />
          </div>
        </div>

        {/* RIGHT: Team rosters */}
        <div className="min-h-[70vh] lg:min-h-0 overflow-hidden">
          <TeamRosterPanel />
        </div>
      </div>
    </div>
  )
}
