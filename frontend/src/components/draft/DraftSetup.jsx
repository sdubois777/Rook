import { useState } from 'react'
import { useDraftStore } from '../../stores/draft'

export default function DraftSetup() {
  const [teamId, setTeamId] = useState('')
  const [draftRoomUrl, setDraftRoomUrl] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const startDraft = useDraftStore((s) => s.startDraft)

  const handleStart = async () => {
    if (!teamId.trim()) return
    setLoading(true)
    setError(null)
    try {
      await startDraft(teamId.trim(), draftRoomUrl.trim() || undefined)
    } catch (err) {
      setError(err.response?.data?.detail || err.message || 'Failed to start draft')
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#0f1117]/90">
      <div className="bg-[#161822] border border-[#2d3148] rounded-xl p-8 w-full max-w-md">
        <h2 className="text-xl font-semibold text-slate-100 mb-6">Start Draft Session</h2>

        <div className="space-y-4">
          <div>
            <label className="block text-sm text-slate-400 mb-1">Your Team ID</label>
            <input
              type="text"
              value={teamId}
              onChange={(e) => setTeamId(e.target.value)}
              placeholder="e.g. team_1"
              className="w-full px-3 py-2 bg-[#1c1f2e] text-slate-200 border border-[#2d3148] rounded-lg focus:outline-none focus:border-blue-500/50 placeholder-slate-600"
              disabled={loading}
            />
          </div>

          <div>
            <label className="block text-sm text-slate-400 mb-1">
              Draft Room URL <span className="text-slate-600">(optional)</span>
            </label>
            <input
              type="text"
              value={draftRoomUrl}
              onChange={(e) => setDraftRoomUrl(e.target.value)}
              placeholder="https://football.fantasysports.yahoo.com/..."
              className="w-full px-3 py-2 bg-[#1c1f2e] text-slate-200 border border-[#2d3148] rounded-lg focus:outline-none focus:border-blue-500/50 placeholder-slate-600"
              disabled={loading}
            />
            <p className="text-xs text-slate-600 mt-1">
              Leave empty for manual frame injection (testing)
            </p>
          </div>

          {error && (
            <div className="text-sm text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg px-3 py-2">
              {error}
            </div>
          )}

          <button
            onClick={handleStart}
            disabled={!teamId.trim() || loading}
            className="w-full py-2.5 bg-blue-600 text-white font-medium rounded-lg hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {loading ? 'Starting...' : 'Start Draft'}
          </button>
        </div>
      </div>
    </div>
  )
}
