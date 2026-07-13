import { useState } from 'react'
import { UserCheck } from 'lucide-react'
import { setMyTeam } from '../api/leagues'

/**
 * Bind-failure RECOVERY — shown when the backend returns 409 error=unbound_team
 * (exact-identity auto-detect matched no team). NOT a free "act as any team"
 * switcher: it asks the one question the system can't infer — "which team is
 * yours?" — and persists the answer to the canonical my_team_id (manual origin),
 * so a later failed auto-bind won't undo it. On success the parent refetches the
 * league and the page renders normally.
 *
 * Props: leagueId, teams [{team_id, name}], onPicked() → parent invalidates its query.
 */
export default function TeamPicker({ leagueId, teams, onPicked }) {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState('')

  const pick = async (teamId) => {
    setBusy(teamId)
    setError('')
    try {
      await setMyTeam(leagueId, teamId)
      onPicked?.()
    } catch {
      setError('Could not save your pick. Please try again.')
      setBusy(null)
    }
  }

  return (
    <div className="mx-auto max-w-2xl p-6">
      <div className="rounded-lg border border-border bg-surface-1 p-6">
        <h2 className="mb-1 flex items-center gap-2 text-lg font-semibold text-white">
          <UserCheck size={20} className="text-brand-accent" /> Which team is yours?
        </h2>
        <p className="mb-4 text-sm text-slate-400">
          We couldn&apos;t automatically match your account to a team in this league.
          Pick your team to continue — we&apos;ll remember it.
        </p>
        <div className="grid gap-2 sm:grid-cols-2">
          {teams.map((t) => (
            <button
              key={t.team_id}
              onClick={() => pick(t.team_id)}
              disabled={busy !== null}
              className="flex min-h-11 items-center justify-between rounded-lg border border-gray-700 bg-surface-2 px-3 py-2 text-left text-sm text-slate-200 transition-colors hover:border-brand disabled:opacity-50"
            >
              <span className="truncate">{t.name || `Team ${t.team_id}`}</span>
              {busy === t.team_id && <span className="text-xs text-slate-500">Saving…</span>}
            </button>
          ))}
        </div>
        {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
      </div>
    </div>
  )
}
