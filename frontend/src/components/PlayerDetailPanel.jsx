import { useQuery } from '@tanstack/react-query'
import { X, Star, StarOff, MessageCircle } from 'lucide-react'
import { fetchPlayer } from '../api/players'
import { useUIStore } from '../stores/ui'
import { usePreferencesStore } from '../stores/preferences'
import { useAssistantStore } from '../stores/assistant'
import PositionBadge from './shared/PositionBadge'
import FlagBadge from './shared/FlagBadge'
import SystemGradeBadge from './shared/SystemGradeBadge'
import ValueComparisonBar from './shared/ValueComparisonBar'

export default function PlayerDetailPanel({ playerId }) {
  const close = useUIStore((s) => s.closePlayerDetail)
  const isWatchlisted = usePreferencesStore((s) => s.watchlist.some((w) => w.player_id === playerId))
  const addToWatchlist = usePreferencesStore((s) => s.addToWatchlist)
  const removeFromWatchlist = usePreferencesStore((s) => s.removeFromWatchlist)
  const prefillForPlayer = useAssistantStore((s) => s.prefillForPlayer)

  const { data: player, isLoading } = useQuery({
    queryKey: ['player', playerId],
    queryFn: () => fetchPlayer(playerId),
  })

  const toggleWatchlist = () => {
    if (isWatchlisted) {
      removeFromWatchlist(playerId)
    } else {
      addToWatchlist(playerId)
    }
  }

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/50 z-40"
        onClick={close}
      />

      {/* Panel */}
      <div className="fixed top-0 right-0 h-full w-[480px] bg-[#161822] border-l border-[#2d3148] z-50 overflow-y-auto shadow-2xl animate-slide-in">
        {/* Header */}
        <div className="sticky top-0 bg-[#161822] border-b border-[#2d3148] px-5 py-4 flex items-center justify-between z-10">
          <div className="flex items-center gap-3">
            {player && <PositionBadge position={player.position} />}
            <h2 className="text-lg font-semibold text-slate-100">
              {player?.name || 'Loading...'}
            </h2>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={toggleWatchlist}
              className="p-1.5 rounded hover:bg-[#222539] transition-colors"
              title={isWatchlisted ? 'Remove from watchlist' : 'Add to watchlist'}
            >
              {isWatchlisted ? (
                <Star size={18} className="text-yellow-400 fill-yellow-400" />
              ) : (
                <StarOff size={18} className="text-slate-500" />
              )}
            </button>
            <button
              onClick={close}
              className="p-1.5 rounded hover:bg-[#222539] transition-colors text-slate-400"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        {isLoading ? (
          <div className="py-20 text-center text-slate-500">Loading player data...</div>
        ) : !player ? (
          <div className="py-20 text-center text-slate-500">Player not found.</div>
        ) : (
          <div className="px-5 py-4 space-y-6">
            {/* Quick stats */}
            <Section title="Overview">
              <div className="grid grid-cols-2 gap-3">
                <Stat label="Team" value={player.team_abbr} />
                <Stat label="Age" value={player.age} />
                <Stat label="Tier" value={player.tier} />
                <Stat label="Situation" value={player.situation_score} />
              </div>
            </Section>

            {/* Valuation */}
            <Section title="Valuation">
              <div className="grid grid-cols-3 gap-3 mb-3">
                <StatBox label="Bid Ceiling" value={`$${player.recommended_bid_ceiling?.toFixed(0) || '--'}`} accent />
                <StatBox label="System" value={`$${player.baseline_value?.toFixed(0) || '--'}`} />
                <StatBox label="Market (FP)" value={`$${player.market_value?.toFixed(0) || '--'}`} />
              </div>
              <div className="grid grid-cols-2 gap-3 mb-3">
                <StatBox label="Ceiling" value={`$${player.ceiling_value?.toFixed(0) || '--'}`} />
                <StatBox label="Floor" value={`$${player.floor_value?.toFixed(0) || '--'}`} />
              </div>
              <ValueComparisonBar
                systemValue={player.baseline_value}
                marketValue={player.market_value}
              />
            </Section>

            {/* Dependency flags */}
            {player.dependencies?.length > 0 && (
              <Section title="Dependency Flags">
                <div className="space-y-2">
                  {player.dependencies.map((dep) => (
                    <div key={dep.id} className="bg-[#1c1f2e] rounded p-3">
                      <div className="flex items-center gap-2 mb-1">
                        <FlagBadge flagType={dep.flag_type} />
                        {dep.confidence && (
                          <span className="text-[10px] text-slate-500">{dep.confidence}</span>
                        )}
                      </div>
                      {dep.trigger_player_name && (
                        <div className="text-xs text-slate-400">
                          Trigger: {dep.trigger_player_name}
                        </div>
                      )}
                      {dep.reasoning && (
                        <div className="text-xs text-slate-500 mt-1">{dep.reasoning}</div>
                      )}
                      {dep.value_impact_pct != null && (
                        <div className={`text-xs mt-1 ${dep.value_impact_pct > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          Impact: {dep.value_impact_pct > 0 ? '+' : ''}{dep.value_impact_pct.toFixed(0)}%
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {/* Team System */}
            {player.team_system && (
              <Section title="Team System">
                <div className="flex items-center gap-3 mb-3">
                  <SystemGradeBadge grade={player.team_system.system_grade} />
                  <div>
                    <div className="text-sm text-slate-300">{player.team_system.qb_name}</div>
                    <div className="text-xs text-slate-500">{player.team_system.oc_scheme}</div>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <Stat label="QB Tier" value={player.team_system.qb_tier} />
                  <Stat label="Pass Pro" value={player.team_system.pass_protection_grade} />
                  <Stat label="Run Block" value={player.team_system.run_blocking_grade} />
                </div>
                {player.team_system.compound_risk_flag && (
                  <div className="mt-2">
                    <FlagBadge flagType="COMPOUND_RISK" />
                  </div>
                )}
              </Section>
            )}

            {/* Profile */}
            {player.profile && (
              <Section title="Player Profile">
                <div className="grid grid-cols-2 gap-2">
                  <Stat label="Role" value={player.profile.role_classification} />
                  <Stat label="Efficiency" value={player.profile.efficiency_signal} />
                  <Stat label="Target Share (3yr)" value={player.profile.target_share_3yr_avg?.toFixed(1) + '%'} />
                  <Stat label="Snap %" value={player.profile.snap_percentage?.toFixed(0) + '%'} />
                  <Stat label="Age Curve" value={player.profile.age_curve_position} />
                  <Stat label="Scarcity" value={player.profile.positional_scarcity_tier} />
                </div>
                {player.profile.breakout_flag && (
                  <div className="mt-2 text-xs text-yellow-400">
                    Breakout candidate: {player.profile.breakout_reasoning}
                  </div>
                )}
              </Section>
            )}

            {/* Injury */}
            {player.injury_profile && (
              <Section title="Injury Risk">
                <div className="grid grid-cols-2 gap-2">
                  <Stat label="Risk Level" value={player.injury_profile.overall_risk_level} />
                  <Stat label="Risk Modifier" value={player.injury_profile.risk_adjusted_value_modifier?.toFixed(2)} />
                </div>
                <div className="flex gap-2 mt-2 flex-wrap">
                  {player.injury_profile.workload_cliff_flag && <FlagBadge flagType="WORKLOAD_CLIFF" compact />}
                  {player.injury_profile.high_mileage_flag && <FlagBadge flagType="HIGH_MILEAGE" compact />}
                  {player.injury_profile.post_acl_flag && <FlagBadge flagType="POST_ACL" compact />}
                  {player.injury_profile.concussion_count > 0 && (
                    <span className="text-[10px] text-amber-400">
                      {player.injury_profile.concussion_count} concussion(s)
                    </span>
                  )}
                </div>
              </Section>
            )}

            {/* Schedule */}
            {player.schedule && (
              <Section title="Schedule">
                <div className="grid grid-cols-2 gap-2">
                  <Stat label="Bye Week" value={`Week ${player.schedule.bye_week || '?'}`} />
                  <Stat label="Score" value={player.schedule.schedule_score?.toFixed(1)} />
                  <Stat label="Early" value={player.schedule.early_window_grade} />
                  <Stat label="Full Season" value={player.schedule.full_season_grade} />
                  <Stat label="Playoffs" value={player.schedule.playoff_window_grade} />
                </div>
                {player.schedule.bye_in_playoff_window && (
                  <div className="text-xs text-amber-400 mt-1">Bye in playoff window</div>
                )}
              </Section>
            )}

            {/* Beat Reporter Signals */}
            {player.beat_signals?.length > 0 && (
              <Section title="Recent Signals">
                <div className="space-y-2">
                  {player.beat_signals.map((sig) => (
                    <div key={sig.id} className="bg-[#1c1f2e] rounded p-2">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-medium text-blue-400">
                          {sig.signal_type.replace(/_/g, ' ')}
                        </span>
                        <span className="text-[10px] text-slate-500">{sig.source}</span>
                      </div>
                      {sig.raw_text && (
                        <div className="text-xs text-slate-400 mt-1">{sig.raw_text}</div>
                      )}
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {/* Notes */}
            {player.notes && (
              <Section title="Notes">
                <p className="text-xs text-slate-400 leading-relaxed">{player.notes}</p>
              </Section>
            )}

            {/* Ask AI Assistant */}
            <button
              onClick={() => prefillForPlayer(
                [playerId],
                `What should I know about ${player.name}?`
              )}
              className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-blue-600/10 border border-blue-500/20 rounded-lg text-blue-400 text-sm hover:bg-blue-600/20 transition-colors"
            >
              <MessageCircle size={14} />
              Ask about this player
            </button>
          </div>
        )}
      </div>
    </>
  )
}

function Section({ title, children }) {
  return (
    <div>
      <h3 className="text-xs uppercase tracking-wider text-slate-500 mb-2 font-medium">
        {title}
      </h3>
      {children}
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

function StatBox({ label, value, accent = false }) {
  return (
    <div className="bg-[#1c1f2e] rounded p-2 text-center">
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className={`text-sm font-mono font-medium ${accent ? 'text-blue-400' : 'text-slate-300'}`}>
        {value}
      </div>
    </div>
  )
}
