import { useDraftStore } from '../stores/draft'
import useDraftSocket from '../hooks/useDraftSocket'
import DraftSetup from '../components/draft/DraftSetup'
import RecommendationPanel from '../components/draft/RecommendationPanel'
import NominationPanel from '../components/draft/NominationPanel'
import MyRoster from '../components/draft/MyRoster'
import AvailablePlayers from '../components/draft/AvailablePlayers'
import OpponentTracker from '../components/draft/OpponentTracker'

const WS_STATUS_LABEL = {
  connected: { text: 'Connected', color: 'bg-emerald-500' },
  reconnecting: { text: 'Reconnecting...', color: 'bg-amber-500' },
  disconnected: { text: 'Disconnected', color: 'bg-red-500' },
}

export default function DraftRoom() {
  const phase = useDraftStore((s) => s.phase)
  const wsStatus = useDraftStore((s) => s.wsStatus)

  // Connect WebSocket
  useDraftSocket()

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
    <div className="h-screen bg-[#0f1117] text-slate-200 flex flex-col">
      {/* Status bar */}
      <div className="flex items-center justify-between px-4 py-1.5 bg-[#161822] border-b border-[#2d3148]">
        <span className="text-sm font-medium text-slate-300">Draft Room</span>
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${statusInfo.color}`} />
          <span className="text-xs text-slate-500">{statusInfo.text}</span>
        </div>
      </div>

      {/* 4-zone grid */}
      <div className="flex-1 grid grid-rows-[35fr_65fr] grid-cols-2 min-h-0">
        {/* Zone 1: Recommendation */}
        <div className="border-r border-b border-[#2d3148] min-h-0 overflow-hidden">
          <RecommendationPanel />
        </div>

        {/* Zone 2: Current Nomination */}
        <div className="border-b border-[#2d3148] min-h-0 overflow-hidden">
          <NominationPanel />
        </div>

        {/* Zone 3: My Roster */}
        <div className="border-r border-[#2d3148] min-h-0 overflow-hidden">
          <MyRoster />
        </div>

        {/* Zone 4: Available Players */}
        <div className="min-h-0 overflow-hidden">
          <AvailablePlayers />
        </div>
      </div>

      {/* Opponent tracker (collapsible sidebar) */}
      <OpponentTracker />
    </div>
  )
}
