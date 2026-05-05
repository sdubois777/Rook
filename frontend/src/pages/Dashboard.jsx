import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Clock, TrendingUp, TrendingDown, AlertTriangle, Star } from 'lucide-react'
import { DRAFT_DATE } from '../lib/theme'
import { fetchPlayers } from '../api/players'
import { fetchNews } from '../api/news'
import { fetchPlayerSummary } from '../api/players'
import { usePreferencesStore } from '../stores/preferences'
import { useUIStore } from '../stores/ui'
import PositionBadge from '../components/shared/PositionBadge'
import PlayerDetailPanel from '../components/PlayerDetailPanel'

function useCountdown() {
  const now = new Date()
  const diff = DRAFT_DATE - now
  if (diff <= 0) return { days: 0, hours: 0, label: 'Draft Day!' }
  const days = Math.floor(diff / (1000 * 60 * 60 * 24))
  const hours = Math.floor((diff % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60))
  return { days, hours, label: `${days}d ${hours}h until draft` }
}

export default function Dashboard() {
  const navigate = useNavigate()
  const countdown = useCountdown()
  const watchlist = usePreferencesStore((s) => s.watchlist)
  const openPlayerDetail = useUIStore((s) => s.openPlayerDetail)
  const selectedPlayerId = useUIStore((s) => s.selectedPlayerId)
  const detailPanelOpen = useUIStore((s) => s.detailPanelOpen)

  // Top value gaps (undervalued)
  const { data: valueData } = useQuery({
    queryKey: ['dashboard-values'],
    queryFn: () => fetchPlayers({ value_gap_dir: 'undervalued', sort: 'value_gap', order: 'desc', per_page: 10 }),
  })

  // Recent signals
  const { data: newsData } = useQuery({
    queryKey: ['dashboard-news'],
    queryFn: () => fetchNews({ days: 7, per_page: 5 }),
  })

  // Position scarcity
  const { data: summaryData } = useQuery({
    queryKey: ['dashboard-summary'],
    queryFn: fetchPlayerSummary,
  })

  // Watchlist players
  const { data: watchlistPlayers } = useQuery({
    queryKey: ['dashboard-watchlist', watchlist.map((w) => w.player_id).join(',')],
    queryFn: () => fetchPlayers({ per_page: 100 }),
    enabled: watchlist.length > 0,
  })

  const watchlistedPlayers = (watchlistPlayers?.players || []).filter((p) =>
    watchlist.some((w) => w.player_id === p.id)
  )

  return (
    <div className="max-w-6xl">
      {/* Countdown bar */}
      <div className="bg-gradient-to-r from-blue-600/20 to-blue-500/10 border border-blue-500/20 rounded-lg px-6 py-4 mb-6 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Clock size={20} className="text-blue-400" />
          <div>
            <div className="text-lg font-semibold text-slate-100">{countdown.label}</div>
            <div className="text-xs text-slate-400">
              Draft: {DRAFT_DATE.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })}
            </div>
          </div>
        </div>
        <div className="text-3xl font-bold text-blue-400 font-mono">
          {countdown.days}<span className="text-sm text-slate-500 ml-1">days</span>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
        {/* Recent Alerts */}
        <DashboardCard title="Recent Alerts" icon={AlertTriangle} iconColor="text-amber-400">
          {(newsData?.signals || []).length === 0 ? (
            <div className="text-sm text-slate-500 py-4">No recent alerts.</div>
          ) : (
            <div className="space-y-2">
              {(newsData?.signals || []).map((sig) => (
                <div key={sig.id} className="flex items-center gap-2 text-sm">
                  <span className="text-xs text-blue-400 font-medium w-24 truncate">
                    {sig.signal_type.replace(/_/g, ' ')}
                  </span>
                  <span className="text-slate-300 truncate">
                    {sig.player_name || 'Unknown'}
                  </span>
                  <span className="text-[10px] text-slate-500 ml-auto">
                    {sig.flagged_at ? new Date(sig.flagged_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : ''}
                  </span>
                </div>
              ))}
              <button
                onClick={() => navigate('/news')}
                className="text-xs text-blue-400 hover:text-blue-300 mt-2"
              >
                View all signals
              </button>
            </div>
          )}
        </DashboardCard>

        {/* Top Value Gaps */}
        <DashboardCard title="Top Value Gaps" icon={TrendingUp} iconColor="text-emerald-400">
          {(valueData?.players || []).length === 0 ? (
            <div className="text-sm text-slate-500 py-4">No value data yet.</div>
          ) : (
            <div className="space-y-1.5">
              {(valueData?.players || []).slice(0, 8).map((p) => (
                <div
                  key={p.id}
                  onClick={() => openPlayerDetail(p.id)}
                  className="flex items-center gap-2 text-sm hover:bg-[#222539] px-2 py-1 rounded cursor-pointer"
                >
                  <PositionBadge position={p.position} />
                  <span className="text-slate-300 truncate flex-1">{p.name}</span>
                  <span className="text-emerald-400 font-mono text-xs">
                    +${p.value_gap?.toFixed(0)}
                  </span>
                </div>
              ))}
              <button
                onClick={() => navigate('/players')}
                className="text-xs text-blue-400 hover:text-blue-300 mt-2"
              >
                View all players
              </button>
            </div>
          )}
        </DashboardCard>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Position Scarcity */}
        <DashboardCard title="Position Scarcity" icon={TrendingDown} iconColor="text-yellow-400">
          {!summaryData ? (
            <div className="text-sm text-slate-500 py-4">Loading...</div>
          ) : (
            <div className="space-y-3">
              {['QB', 'RB', 'WR', 'TE'].map((pos) => {
                const counts = summaryData.position_counts?.[pos]
                if (!counts) return null
                const maxTotal = Math.max(
                  ...Object.values(summaryData.position_counts || {}).map((c) => c.total || 1)
                )
                const pct = ((counts.total / maxTotal) * 100).toFixed(0)
                return (
                  <div key={pos}>
                    <div className="flex justify-between text-xs mb-1">
                      <span className="text-slate-400">{pos}</span>
                      <span className="text-slate-300">
                        T1: {counts.tier1} / T2: {counts.tier2} / Total: {counts.total}
                      </span>
                    </div>
                    <div className="h-2 bg-[#1c1f2e] rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all ${
                          pos === 'QB' ? 'bg-purple-500' :
                          pos === 'RB' ? 'bg-emerald-500' :
                          pos === 'WR' ? 'bg-blue-500' : 'bg-orange-500'
                        }`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </DashboardCard>

        {/* Watchlist */}
        <DashboardCard title="Watchlist" icon={Star} iconColor="text-yellow-400">
          {watchlistedPlayers.length === 0 ? (
            <div className="text-sm text-slate-500 py-4">
              No players in watchlist yet. Star players to track them here.
            </div>
          ) : (
            <div className="space-y-1.5">
              {watchlistedPlayers.slice(0, 8).map((p) => (
                <div
                  key={p.id}
                  onClick={() => openPlayerDetail(p.id)}
                  className="flex items-center gap-2 text-sm hover:bg-[#222539] px-2 py-1 rounded cursor-pointer"
                >
                  <PositionBadge position={p.position} />
                  <span className="text-slate-300 truncate flex-1">{p.name}</span>
                  <span className="text-blue-400 font-mono text-xs">
                    ${p.recommended_bid_ceiling?.toFixed(0) || '--'}
                  </span>
                </div>
              ))}
            </div>
          )}
        </DashboardCard>
      </div>

      {/* Detail panel */}
      {detailPanelOpen && selectedPlayerId && (
        <PlayerDetailPanel playerId={selectedPlayerId} />
      )}
    </div>
  )
}

function DashboardCard({ title, icon: Icon, iconColor, children }) {
  return (
    <div className="bg-[#161822] rounded-lg border border-[#2d3148] p-4">
      <div className="flex items-center gap-2 mb-3">
        <Icon size={16} className={iconColor} />
        <h3 className="text-sm font-medium text-slate-200">{title}</h3>
      </div>
      {children}
    </div>
  )
}
