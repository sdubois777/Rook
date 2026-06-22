import { useEffect, useState } from 'react'
import { useAuth } from '@clerk/clerk-react'
import { useLeague } from '../../context/LeagueContext'
import { fetchUserLeagues } from '../../api/league'
import { useUIStore } from '../../stores/ui'

// Short chip label for the collapsed sidebar: initials of the league name.
function abbreviate(name) {
  if (!name) return '—'
  const words = name.trim().split(/\s+/).filter(Boolean)
  return words.slice(0, 3).map((w) => w[0].toUpperCase()).join('')
}

export default function LeagueSelector() {
  const collapsed = useUIStore((s) => s.sidebarCollapsed)
  const { isLoaded, isSignedIn } = useAuth()
  const { selectedLeague, setSelectedLeague } = useLeague()
  const [leagues, setLeagues] = useState([])

  useEffect(() => {
    // Gate on Clerk being ready — otherwise the fetch goes out tokenless on a
    // hard refresh and 401s. (A transient 401 is still retried by the client
    // interceptor.) Re-runs when auth becomes ready.
    if (!isLoaded || !isSignedIn) return
    let cancelled = false
    fetchUserLeagues()
      .then((data) => {
        if (cancelled) return
        const list = data || []
        setLeagues(list)
        // Auto-select: if nothing saved (or the saved one is gone), pick the
        // first connected league so the app always has a league in context.
        const stillValid = list.some((l) => l.id === selectedLeague?.id)
        if (list.length > 0 && !stillValid) setSelectedLeague(list[0])
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
    // selectedLeague?.id intentionally omitted — run once auth is ready;
    // re-running on selection change would fight the user's pick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLoaded, isSignedIn, setSelectedLeague])

  // Fall back to the saved league so the selector never renders empty before
  // (or after a failed) fetch — the localStorage value is available immediately.
  const options = leagues.length > 0 ? leagues : selectedLeague ? [selectedLeague] : []

  if (options.length === 0) return null

  const meta = selectedLeague
    ? `${selectedLeague.draft_type} · ${selectedLeague.scoring} · ${selectedLeague.team_count}-tm`
    : null

  // Full dropdown — always used on mobile (the drawer is full-width, so the
  // desktop "collapsed" rail concept doesn't apply there).
  const fullSelector = (
    <div className="px-3 py-2 border-b border-border">
      <select
        aria-label="Select league"
        value={selectedLeague?.id || ''}
        onChange={(e) => {
          const next = options.find((l) => l.id === e.target.value) || null
          setSelectedLeague(next)
        }}
        className="w-full bg-surface-2 text-slate-200 border border-border rounded px-2 py-1.5 text-xs"
      >
        {options.map((l) => (
          <option key={l.id} value={l.id}>
            {l.league_name || l.league_id}
          </option>
        ))}
      </select>
      {meta && (
        <div className="mt-1 text-[10px] uppercase tracking-wide text-slate-500">
          {meta}
        </div>
      )}
    </div>
  )

  // Desktop-collapsed rail: show the initials chip only at lg; the mobile drawer
  // still gets the full dropdown.
  if (collapsed) {
    return (
      <>
        <div className="hidden lg:flex px-2 py-2 border-b border-border justify-center">
          <span
            title={selectedLeague?.league_name || 'No league'}
            className="text-[10px] font-semibold text-blue-300 bg-surface-2 rounded px-1.5 py-1"
          >
            {abbreviate(selectedLeague?.league_name)}
          </span>
        </div>
        <div className="lg:hidden">{fullSelector}</div>
      </>
    )
  }

  return fullSelector
}
