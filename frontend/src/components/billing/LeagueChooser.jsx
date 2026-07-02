import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchLeagueLimitState, resolveLeagueLimit } from '../../api/leagues'
import { SCORING_LABELS } from '../../lib/constants'

/**
 * Forced over-limit chooser. Self-gates on the limit-state: renders nothing unless
 * the account is over its active-league cap (after a downgrade). The user picks up
 * to `cap` current-season leagues to keep active; the rest are parked (kept as
 * history, restored on re-upgrade) — never deleted.
 */
export default function LeagueChooser() {
  const qc = useQueryClient()
  const { data } = useQuery({
    queryKey: ['league-limit-state'],
    queryFn: fetchLeagueLimitState,
  })
  const [keep, setKeep] = useState(null) // Set<id> | null (null = derive default)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  if (!data || !data.over_limit) return null

  const cap = data.max_leagues ?? 0
  const candidates = data.candidates || []
  const selected =
    keep ??
    new Set(candidates.filter((c) => !c.suspended).slice(0, cap).map((c) => c.id))

  const toggle = (id) => {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else if (next.size < cap) next.add(id)
    setKeep(next)
  }

  const submit = async () => {
    setBusy(true)
    setError('')
    try {
      await resolveLeagueLimit([...selected])
      qc.invalidateQueries({ queryKey: ['league-limit-state'] })
      qc.invalidateQueries({ queryKey: ['account'] })
      qc.invalidateQueries({ queryKey: ['me'] })
    } catch {
      setError('Could not save your choice. Please try again.')
      setBusy(false)
    }
  }

  return (
    <section className="bg-yellow-500/10 border border-yellow-500/40 rounded-xl p-6 mb-6">
      <h2 className="text-lg font-semibold text-yellow-300 mb-1">
        Choose your active leagues
      </h2>
      <p className="text-sm text-gray-300 mb-4">
        Your plan allows {cap} active {cap === 1 ? 'league' : 'leagues'}, but you have{' '}
        {data.active_count}. Pick up to {cap} to keep active — the rest are parked
        (kept as history and reactivated when you upgrade). Nothing is deleted.
      </p>

      <div className="space-y-2 mb-4">
        {candidates.map((lg) => {
          const checked = selected.has(lg.id)
          const full = !checked && selected.size >= cap
          return (
            <label
              key={lg.id}
              className={`flex items-center gap-3 rounded-lg border px-3 py-2 cursor-pointer transition-colors ${
                checked
                  ? 'border-brand bg-brand/10'
                  : full
                    ? 'border-gray-800 bg-gray-900 opacity-50 cursor-not-allowed'
                    : 'border-gray-700 bg-gray-800 hover:border-gray-600'
              }`}
            >
              <input
                type="checkbox"
                checked={checked}
                disabled={full}
                onChange={() => toggle(lg.id)}
                className="accent-brand"
              />
              <div className="min-w-0">
                <div className="text-sm font-medium text-white truncate">
                  {lg.league_name || lg.league_id}
                </div>
                <div className="text-xs text-gray-400">
                  {lg.platform} · {SCORING_LABELS[lg.scoring] || lg.scoring} ·{' '}
                  {lg.season_year}
                  {lg.suspended && <span className="text-yellow-500"> · currently parked</span>}
                </div>
              </div>
            </label>
          )
        })}
      </div>

      {error && <p className="text-sm text-red-400 mb-2">{error}</p>}

      <button
        onClick={submit}
        disabled={busy || selected.size === 0 || selected.size > cap}
        className="bg-brand hover:bg-brand-hover disabled:opacity-50 text-white text-sm font-semibold px-4 py-2 rounded-lg transition-colors"
      >
        {busy ? 'Saving…' : `Keep ${selected.size} of ${cap} active`}
      </button>
    </section>
  )
}
