import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { fetchTeams } from '../api/teams'
import SystemGradeBadge from '../components/shared/SystemGradeBadge'

export default function Teams() {
  const navigate = useNavigate()
  const { data, isLoading } = useQuery({
    queryKey: ['teams'],
    queryFn: () => fetchTeams(),
  })

  const teams = data?.teams || []

  return (
    <div className="max-w-5xl">
      <h1 className="text-2xl font-semibold text-slate-100 mb-4">NFL Teams</h1>

      <div className="bg-[#161822] rounded-lg border border-[#2d3148] overflow-hidden">
        {/* Header */}
        <div className="grid grid-cols-[60px_80px_1fr_100px_80px_80px_80px_60px] gap-2 px-4 py-2 border-b border-[#2d3148] text-[10px] uppercase tracking-wider text-slate-500">
          <span>Grade</span>
          <span>Team</span>
          <span>QB</span>
          <span>Scheme</span>
          <span>Pass Pro</span>
          <span>Run Block</span>
          <span>QB Tier</span>
          <span className="text-right">Players</span>
        </div>

        {isLoading ? (
          <div className="py-12 text-center text-slate-500 text-sm">Loading...</div>
        ) : teams.length === 0 ? (
          <div className="py-12 text-center text-slate-500 text-sm">No team data found.</div>
        ) : (
          teams.map((team) => (
            <div
              key={team.team_abbr}
              onClick={() => navigate(`/teams/${team.team_abbr.toLowerCase()}`)}
              className="grid grid-cols-[60px_80px_1fr_100px_80px_80px_80px_60px] gap-2 px-4 py-2.5 items-center hover:bg-[#222539] cursor-pointer transition-colors border-b border-[#2d3148]/50"
            >
              <SystemGradeBadge grade={team.system_grade} size="sm" />
              <span className="text-sm font-medium text-slate-200">{team.team_abbr}</span>
              <span className="text-sm text-slate-300 truncate">{team.qb_name || '--'}</span>
              <span className="text-xs text-slate-400 truncate">{team.oc_scheme || '--'}</span>
              <GradeCell grade={team.pass_protection_grade} />
              <GradeCell grade={team.run_blocking_grade} />
              <span className="text-xs text-slate-400">{team.qb_tier || '--'}</span>
              <span className="text-xs text-slate-500 text-right">{team.player_count}</span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}

function GradeCell({ grade }) {
  if (!grade) return <span className="text-xs text-slate-500">--</span>

  const letter = grade.charAt(0)
  const colorMap = { A: 'text-emerald-400', B: 'text-teal-400', C: 'text-yellow-400', D: 'text-orange-400', F: 'text-red-400' }

  return (
    <span className={`text-xs font-medium ${colorMap[letter] || 'text-slate-400'}`}>
      {grade}
    </span>
  )
}
