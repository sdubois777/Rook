import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { fetchTeams } from '../api/teams'
import SystemGradeBadge from '../components/shared/SystemGradeBadge'

const NFL_DIVISIONS = {
  'AFC East': ['BUF', 'MIA', 'NE', 'NYJ'],
  'AFC North': ['BAL', 'CIN', 'CLE', 'PIT'],
  'AFC South': ['HOU', 'IND', 'JAX', 'TEN'],
  'AFC West': ['DEN', 'KC', 'LAC', 'LV'],
  'NFC East': ['DAL', 'NYG', 'PHI', 'WAS'],
  'NFC North': ['CHI', 'DET', 'GB', 'MIN'],
  'NFC South': ['ATL', 'CAR', 'NO', 'TB'],
  'NFC West': ['ARI', 'LAR', 'SEA', 'SF'],
}

export default function Teams() {
  const navigate = useNavigate()
  const [division, setDivision] = useState('')
  const { data, isLoading } = useQuery({
    queryKey: ['teams'],
    queryFn: () => fetchTeams(),
  })

  let teams = data?.teams || []
  if (division) {
    const divTeams = NFL_DIVISIONS[division] || []
    teams = teams.filter((t) => divTeams.includes(t.team_abbr))
  }

  return (
    <div className="max-w-5xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-semibold text-slate-100">NFL Teams</h1>
        <select
          value={division}
          onChange={(e) => setDivision(e.target.value)}
          className="bg-[#1c1f2e] text-sm text-slate-300 border border-[#2d3148] rounded px-3 py-1.5 focus:outline-none focus:border-blue-500/50"
        >
          <option value="">All Divisions</option>
          {Object.keys(NFL_DIVISIONS).map((div) => (
            <option key={div} value={div}>{div}</option>
          ))}
        </select>
      </div>

      <div className="bg-[#161822] rounded-lg border border-[#2d3148] overflow-hidden">
        {/* Header — column template grows with the breakpoint so hidden cells
            don't leave empty grid tracks. Grade/Team/QB + the two O-line grades
            (Pass Pro, Run Block) always show; Scheme at sm; QB Tier + Players at
            md; full desktop template at md (exact at lg). */}
        <div className="grid grid-cols-[40px_40px_1fr_56px_56px] sm:grid-cols-[52px_60px_1fr_90px_64px_64px] md:grid-cols-[60px_80px_1fr_100px_80px_80px_80px_60px] gap-2 px-4 py-2 border-b border-[#2d3148] text-[10px] uppercase tracking-wider text-slate-500">
          <span>Grade</span>
          <span>Team</span>
          <span>QB</span>
          <span className="hidden sm:block">Scheme</span>
          <span>Pass Pro</span>
          <span>Run Block</span>
          <span className="hidden md:block">QB Tier</span>
          <span className="hidden md:block text-right">Players</span>
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
              className="grid grid-cols-[40px_40px_1fr_56px_56px] sm:grid-cols-[52px_60px_1fr_90px_64px_64px] md:grid-cols-[60px_80px_1fr_100px_80px_80px_80px_60px] gap-2 px-4 py-2.5 items-center hover:bg-[#222539] cursor-pointer transition-colors border-b border-[#2d3148]/50"
            >
              <SystemGradeBadge grade={team.system_grade} size="sm" />
              <span className="text-sm font-medium text-slate-200">{team.team_abbr}</span>
              <span className="text-sm text-slate-300 truncate">{team.qb_name || '--'}</span>
              <span className="hidden sm:block text-xs text-slate-400 truncate">{team.oc_scheme || '--'}</span>
              <GradeCell grade={team.pass_protection_grade} />
              <GradeCell grade={team.run_blocking_grade} />
              <span className="hidden md:block text-xs text-slate-400">{team.qb_tier || '--'}</span>
              <span className="hidden md:block text-xs text-slate-500 text-right">{team.player_count}</span>
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
