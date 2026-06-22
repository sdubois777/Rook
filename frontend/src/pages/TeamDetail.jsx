import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { fetchTeam } from '../api/teams'
import { useUIStore } from '../stores/ui'
import SystemGradeBadge from '../components/shared/SystemGradeBadge'
import PositionBadge from '../components/shared/PositionBadge'
import FlagBadge from '../components/shared/FlagBadge'
import PlayerDetailPanel from '../components/PlayerDetailPanel'

export default function TeamDetail() {
  const { abbr } = useParams()
  const navigate = useNavigate()
  const openPlayerDetail = useUIStore((s) => s.openPlayerDetail)
  const selectedPlayerId = useUIStore((s) => s.selectedPlayerId)
  const detailPanelOpen = useUIStore((s) => s.detailPanelOpen)

  const { data: team, isLoading } = useQuery({
    queryKey: ['team', abbr],
    queryFn: () => fetchTeam(abbr),
  })

  if (isLoading) {
    return <div className="py-20 text-center text-slate-500">Loading team...</div>
  }

  if (!team) {
    return <div className="py-20 text-center text-slate-500">Team not found.</div>
  }

  return (
    <div className="max-w-5xl">
      {/* Back button */}
      <button
        onClick={() => navigate('/teams')}
        className="flex items-center gap-1 text-sm text-slate-400 hover:text-slate-200 mb-4"
      >
        <ArrowLeft size={16} />
        All Teams
      </button>

      {/* Header */}
      <div className="flex items-center gap-4 mb-6">
        <SystemGradeBadge grade={team.system_grade} size="lg" />
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">{team.team_abbr}</h1>
          <div className="text-sm text-slate-400">
            {team.season_year} Season{team.system_ceiling && ` — Ceiling: ${team.system_ceiling}`}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
        {/* QB Section */}
        <div className="bg-surface-1 rounded-lg border border-border p-4">
          <h3 className="text-xs uppercase tracking-wider text-slate-500 mb-3 font-medium">
            Quarterback
          </h3>
          <div className="text-lg font-medium text-slate-200 mb-2">{team.qb_name || '--'}</div>
          <div className="grid grid-cols-2 gap-2 text-sm">
            <Stat label="Tier" value={team.qb_tier} />
            <Stat label="Experience" value={team.qb_experience_years ? `${team.qb_experience_years} yrs` : '--'} />
            <Stat label="CPOE" value={team.qb_cpoe?.toFixed(1)} />
            <Stat label="Air Yards/Att" value={team.qb_air_yards_per_attempt?.toFixed(1)} />
            <Stat label="Pressure Perf" value={team.qb_pressure_performance} />
            <Stat label="Downfield" value={team.qb_downfield_aggressiveness} />
          </div>
          {team.qb_wr_trust_score != null && (
            <div className="mt-3 pt-3 border-t border-border">
              <div className="flex items-center justify-between">
                <span className="text-[10px] text-slate-500">QB→WR Trust</span>
                <span className={`text-sm font-mono font-medium ${
                  team.qb_wr_trust_score >= 70 ? 'text-emerald-400'
                    : team.qb_wr_trust_score >= 50 ? 'text-amber-400'
                    : 'text-red-400'
                }`}>
                  {team.qb_wr_trust_score}/100
                </span>
              </div>
              <div className="h-1.5 bg-surface-2 rounded-full overflow-hidden mt-1">
                <div
                  className={`h-full rounded-full ${
                    team.qb_wr_trust_score >= 70 ? 'bg-emerald-500'
                      : team.qb_wr_trust_score >= 50 ? 'bg-amber-500'
                      : 'bg-red-500'
                  }`}
                  style={{ width: `${team.qb_wr_trust_score}%` }}
                />
              </div>
            </div>
          )}
          <div className="flex gap-2 mt-3">
            {team.rookie_qb_flag && <FlagBadge flagType="ROOKIE_QB" compact />}
            {team.compound_risk_flag && <FlagBadge flagType="COMPOUND_RISK" compact />}
          </div>
        </div>

        {/* O-Line Section */}
        <div className="bg-surface-1 rounded-lg border border-border p-4">
          <h3 className="text-xs uppercase tracking-wider text-slate-500 mb-3 font-medium">
            Offensive Line
          </h3>
          <div className="space-y-4">
            <GradeBar label="Pass Protection" grade={team.pass_protection_grade} />
            <GradeBar label="Run Blocking" grade={team.run_blocking_grade} />
          </div>
        </div>

        {/* OC Section */}
        <div className="bg-surface-1 rounded-lg border border-border p-4">
          <h3 className="text-xs uppercase tracking-wider text-slate-500 mb-3 font-medium">
            Offensive Coordinator
          </h3>
          <div className="text-lg font-medium text-slate-200 mb-2">{team.oc_name || '--'}</div>
          <div className="grid grid-cols-1 gap-2 text-sm">
            <Stat label="Scheme" value={team.oc_scheme} />
            <Stat label="Run/Pass Split" value={team.oc_run_pass_split_tendency ? `${(team.oc_run_pass_split_tendency * 100).toFixed(0)}% pass` : '--'} />
            <Stat label="Personnel" value={team.personnel_tendency} />
            <Stat label="Red Zone" value={team.red_zone_philosophy} />
          </div>
        </div>
      </div>

      {/* Notes */}
      {team.notes && (
        <div className="bg-surface-1 rounded-lg border border-border p-4 mb-6">
          <h3 className="text-xs uppercase tracking-wider text-slate-500 mb-2 font-medium">
            System Notes
          </h3>
          <p className="text-sm text-slate-400 leading-relaxed">{team.notes}</p>
        </div>
      )}

      {/* Skill Position Players */}
      <div className="bg-surface-1 rounded-lg border border-border overflow-hidden">
        <div className="px-4 py-3 border-b border-border">
          <h3 className="text-sm font-medium text-slate-200">
            Skill Position Players ({team.players?.length || 0})
          </h3>
        </div>

        {/* Responsive column-hiding: Pos/Player + Gap (value signal) + Flag
            always show; Tier + Ceiling at sm; Market at lg (desktop exact). */}
        <div className="grid grid-cols-[36px_1fr_64px_72px] sm:grid-cols-[40px_1fr_56px_72px_72px_88px] lg:grid-cols-[40px_1fr_60px_80px_80px_80px_100px] gap-2 px-4 py-2 text-[10px] uppercase tracking-wider text-slate-500 border-b border-border">
          <span>Pos</span>
          <span>Player</span>
          <span className="hidden sm:block">Tier</span>
          <span className="hidden sm:block text-right">Ceiling</span>
          <span className="hidden lg:block text-right">Market</span>
          <span className="text-right">Gap</span>
          <span>Flag</span>
        </div>

        {(team.players || []).map((p) => (
          <div
            key={p.id}
            onClick={() => openPlayerDetail(p.id)}
            className="grid grid-cols-[36px_1fr_64px_72px] sm:grid-cols-[40px_1fr_56px_72px_72px_88px] lg:grid-cols-[40px_1fr_60px_80px_80px_80px_100px] gap-2 px-4 py-2.5 items-center hover:bg-surface-3 cursor-pointer transition-colors border-b border-border/50"
          >
            <PositionBadge position={p.position} />
            <span className="text-sm text-slate-200 font-medium truncate">{p.name}</span>
            <span className="hidden sm:block text-xs text-slate-400">T{p.tier || '?'}</span>
            <span className="hidden sm:block text-sm text-blue-400 font-mono text-right">
              {p.recommended_bid_ceiling != null ? `$${p.recommended_bid_ceiling.toFixed(0)}` : '--'}
            </span>
            <span className="hidden lg:block text-sm text-slate-300 font-mono text-right">
              {p.market_value != null ? `$${p.market_value.toFixed(0)}` : '--'}
            </span>
            <span className={`text-sm font-mono text-right ${
              p.value_gap > 3 ? 'text-emerald-400' : p.value_gap < -3 ? 'text-red-400' : 'text-slate-400'
            }`}>
              {p.value_gap != null ? `${p.value_gap > 0 ? '+' : ''}$${p.value_gap.toFixed(0)}` : '--'}
            </span>
            {p.top_flag ? <FlagBadge flagType={p.top_flag} compact /> : <span />}
          </div>
        ))}

        {(!team.players || team.players.length === 0) && (
          <div className="py-8 text-center text-slate-500 text-sm">No skill players found.</div>
        )}
      </div>

      {/* Detail panel */}
      {detailPanelOpen && selectedPlayerId && (
        <PlayerDetailPanel playerId={selectedPlayerId} />
      )}
    </div>
  )
}

function Stat({ label, value }) {
  return (
    <div>
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className="text-sm text-slate-300">{value || '--'}</div>
    </div>
  )
}

function GradeBar({ label, grade }) {
  const gradeValues = { 'A+': 95, A: 90, 'A-': 85, 'B+': 80, B: 75, 'B-': 70, 'C+': 65, C: 60, 'C-': 55, 'D+': 50, D: 45, 'D-': 40, F: 30 }
  const pct = gradeValues[grade] || 50
  const letter = grade?.charAt(0) || 'C'
  const colorMap = { A: 'bg-emerald-500', B: 'bg-teal-500', C: 'bg-yellow-500', D: 'bg-orange-500', F: 'bg-red-500' }
  const barColor = colorMap[letter] || 'bg-slate-500'

  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span className="text-slate-400">{label}</span>
        <span className="text-slate-300 font-medium">{grade || '--'}</span>
      </div>
      <div className="h-2 bg-surface-2 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full ${barColor} transition-all`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}
