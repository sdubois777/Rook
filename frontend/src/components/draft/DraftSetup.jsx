import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useDraftStore } from '../../stores/draft'
import { useLeague } from '../../context/LeagueContext'
import { useEntitlements, isFeatureLocked } from '../../hooks/useEntitlements'

export default function DraftSetup() {
  const [teamId, setTeamId] = useState('')
  const [draftRoomUrl, setDraftRoomUrl] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const startDraft = useDraftStore((s) => s.startDraft)
  const { selectedLeague } = useLeague()
  const { tierLimits } = useEntitlements()
  // Live draft is a standard+ entitlement. Show a locked state instead of a
  // dead button when we KNOW the tier lacks it (fail-open otherwise). The
  // backend gate remains the real boundary — this is affordance only.
  const liveDraftLocked = isFeatureLocked(tierLimits, 'live_draft')

  const handleStart = async () => {
    if (!teamId.trim()) return
    setLoading(true)
    setError(null)
    try {
      // Pass the selected league's type so the engine picks the snake vs
      // auction path (and loads league settings via league_id).
      await startDraft(teamId.trim(), draftRoomUrl.trim() || undefined, {
        leagueId: selectedLeague?.id,
        draftType: selectedLeague?.draft_type || 'auction',
      })
    } catch (err) {
      setError(err.response?.data?.detail || err.message || 'Failed to start draft')
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-surface-0/90">
      <div className="bg-surface-1 border border-border rounded-xl p-8 w-full max-w-md">
        <h2 className="text-xl font-semibold text-slate-100 mb-6">Start Draft Session</h2>

        <div className="space-y-4">
          <div>
            <label className="block text-sm text-slate-400 mb-1">Your Team Name</label>
            <input
              type="text"
              value={teamId}
              onChange={(e) => setTeamId(e.target.value)}
              placeholder="Stephen — exactly as in the draft room"
              className="w-full px-3 py-2 bg-surface-2 text-slate-200 border border-border rounded-lg focus:outline-none focus:border-brand-accent/60 placeholder-slate-600"
              disabled={loading}
            />
            <p className="text-xs text-slate-600 mt-1">
              Must match your team name in the draft room's team list exactly —
              this is how Rook detects the players you win.
            </p>
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
              className="w-full px-3 py-2 bg-surface-2 text-slate-200 border border-border rounded-lg focus:outline-none focus:border-brand-accent/60 placeholder-slate-600"
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

          {liveDraftLocked ? (
            <div className="rounded-lg border border-border bg-surface-2 px-4 py-3 text-center">
              <p className="text-sm text-slate-300">
                🔒 Live draft is a <span className="font-semibold">Standard</span> feature.
              </p>
              <Link
                to="/pricing"
                className="mt-3 inline-block py-2 px-4 bg-brand text-white text-sm font-medium rounded-lg hover:bg-brand-hover transition-colors"
              >
                Upgrade to unlock
              </Link>
            </div>
          ) : (
            <button
              onClick={handleStart}
              disabled={!teamId.trim() || loading}
              className="w-full py-2.5 bg-brand text-white font-medium rounded-lg hover:bg-brand-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {loading ? 'Starting...' : 'Start Draft'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
