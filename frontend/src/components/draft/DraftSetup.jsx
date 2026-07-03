import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useDraftStore } from '../../stores/draft'
import { useLeague } from '../../context/LeagueContext'
import { useEntitlements, isFeatureLocked } from '../../hooks/useEntitlements'

export default function DraftSetup() {
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
    setLoading(true)
    setError(null)
    try {
      // No team name needed — the extension self-identifies your team and Rook
      // derives the label. Pass the selected league so the engine picks the
      // snake vs auction path and loads league settings.
      await startDraft({
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
        <h2 className="text-xl font-semibold text-slate-100 mb-2">Start Draft Session</h2>
        <p className="text-sm text-slate-400 mb-6">
          Rook detects your team automatically from the draft room — just start the
          engine and open your draft in the browser with the extension active.
        </p>

        <div className="space-y-4">
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
                to="/account"
                className="mt-3 inline-block py-2 px-4 bg-brand text-white text-sm font-medium rounded-lg hover:bg-brand-hover transition-colors"
              >
                Upgrade to unlock
              </Link>
            </div>
          ) : (
            <button
              onClick={handleStart}
              disabled={loading}
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
