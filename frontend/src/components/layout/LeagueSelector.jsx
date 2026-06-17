import { useEffect, useState } from 'react'
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
  const { selectedLeague, setSelectedLeague } = useLeague()
  const [leagues, setLeagues] = useState([])

  useEffect(() => {
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
    // selectedLeague?.id intentionally omitted — run once on mount; re-running
    // on selection change would fight the user's pick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setSelectedLeague])

  if (leagues.length === 0) return null

  if (collapsed) {
    return (
      <div className="px-2 py-2 border-b border-[#2d3148] flex justify-center">
        <span
          title={selectedLeague?.league_name || 'No league'}
          className="text-[10px] font-semibold text-blue-300 bg-[#1c1f2e] rounded px-1.5 py-1"
        >
          {abbreviate(selectedLeague?.league_name)}
        </span>
      </div>
    )
  }

  const meta = selectedLeague
    ? `${selectedLeague.draft_type} · ${selectedLeague.scoring} · ${selectedLeague.team_count}-tm`
    : null

  return (
    <div className="px-3 py-2 border-b border-[#2d3148]">
      <select
        aria-label="Select league"
        value={selectedLeague?.id || ''}
        onChange={(e) => {
          const next = leagues.find((l) => l.id === e.target.value) || null
          setSelectedLeague(next)
        }}
        className="w-full bg-[#1c1f2e] text-slate-200 border border-[#2d3148] rounded px-2 py-1.5 text-xs"
      >
        {leagues.map((l) => (
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
}
