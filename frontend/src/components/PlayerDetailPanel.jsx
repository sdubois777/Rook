import { useQuery } from '@tanstack/react-query'
import { X, Star, StarOff, MessageCircle, Info } from 'lucide-react'
import { fetchPlayer } from '../api/players'
import { useUIStore } from '../stores/ui'
import { usePreferencesStore } from '../stores/preferences'
import { useAssistantStore } from '../stores/assistant'
import PositionBadge from './shared/PositionBadge'
import FlagBadge from './shared/FlagBadge'
import SystemGradeBadge from './shared/SystemGradeBadge'
import ValueComparisonBar from './shared/ValueComparisonBar'
import { getDisplaySignal, getSignalBadgeStyle, getSignalLabel } from '../lib/signals'
import { useLeague } from '../context/LeagueContext'

const SNAKE_FLAG_STYLE = {
  VALUE: 'text-emerald-400 bg-emerald-500/15',
  SLEEPER: 'text-purple-400 bg-purple-500/15',
  TARGET: 'text-blue-400 bg-blue-500/15',
  REACH: 'text-orange-400 bg-orange-500/15',
}

export default function PlayerDetailPanel({ playerId, onPlayerSelect }) {
  const { isSnake } = useLeague()
  const close = useUIStore((s) => s.closePlayerDetail)
  const openPlayerDetail = onPlayerSelect || useUIStore((s) => s.openPlayerDetail)
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
              {/* Snake: ADP fields + flag, no dollar amounts */}
              {isSnake && (
                <>
                  <div className="grid grid-cols-3 gap-3 mb-3">
                    <StatBox label="AI ADP" value={player.adp_rank != null ? `#${player.adp_rank}` : '--'} accent />
                    <StatBox label="FP ADP" value={player.adp_fantasypros != null ? player.adp_fantasypros.toFixed(1) : '--'} />
                    <StatBox
                      label="Diff"
                      value={
                        player.adp_diff != null
                          ? `${player.adp_diff > 0 ? '+' : ''}${player.adp_diff.toFixed(0)}`
                          : '--'
                      }
                    />
                  </div>
                  {player.snake_flag && (
                    <div className="mb-3">
                      <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${SNAKE_FLAG_STYLE[player.snake_flag] || 'text-slate-400 bg-slate-500/15'}`}>
                        {player.snake_flag}
                      </span>
                    </div>
                  )}
                  {player.auction_note && (
                    <p className="text-xs text-slate-400 leading-relaxed mb-2">
                      {player.auction_note}
                    </p>
                  )}
                </>
              )}

              {/* Auction: the dollar valuation block (hidden for snake leagues) */}
              {!isSnake && (
              <>
              <div className={`grid ${player.ai_bid_ceiling != null ? 'grid-cols-4' : 'grid-cols-3'} gap-3 mb-3`}>
                <StatBox label="Bid Ceiling" value={`$${player.recommended_bid_ceiling?.toFixed(0) || '--'}`} accent />
                {player.ai_bid_ceiling != null && (
                  <StatBox label="AI Ceiling" value={`$${player.ai_bid_ceiling}`} accent />
                )}
                <StatBox label="System" value={`$${player.baseline_value?.toFixed(0) || '--'}`} />
                <StatBox
                  label={`${player.market_value_season || ''} ADP`}
                  value={`$${player.market_value?.toFixed(0) || '--'}`}
                />
              </div>

              {/* AI confidence range */}
              {player.ai_confidence_floor != null && player.ai_confidence_ceiling != null && (
                <div className="bg-[#1c1f2e] rounded p-2 mb-3 text-center">
                  <div className="text-[10px] text-slate-500 mb-1">Confidence Range</div>
                  <div className="text-sm font-mono text-slate-300">
                    ${player.ai_confidence_floor} &ndash; ${player.ai_confidence_ceiling}
                  </div>
                </div>
              )}

              <div className="grid grid-cols-2 gap-3 mb-3">
                <StatBox label="Ceiling" value={`$${player.ceiling_value?.toFixed(0) || '--'}`} />
                <StatBox label="Floor" value={`$${player.floor_value?.toFixed(0) || '--'}`} />
              </div>

              {player.prior_season_price != null && (
                <div className="bg-[#1c1f2e] rounded p-2 mb-3 flex items-center justify-between">
                  <span className="text-[10px] text-slate-500">{player.prior_season_year} Avg Price</span>
                  <span className="text-sm font-mono text-slate-300">${player.prior_season_price.toFixed(0)}</span>
                </div>
              )}

              {/* AI Assessment + tactical badges */}
              {(player.value_assessment || player.pay_up_flag || player.nomination_target_flag) && (
                <div className="mb-3">
                  <div className="flex items-center gap-2 flex-wrap mb-2">
                    {player.value_assessment && (() => {
                      const signal = getDisplaySignal(player)
                      return (
                        <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${getSignalBadgeStyle(signal)}`}>
                          {getSignalLabel(signal)}
                        </span>
                      )
                    })()}
                    {player.pay_up_flag && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-emerald-500/15 text-emerald-400">
                        PAY UP
                      </span>
                    )}
                    {player.nomination_target_flag && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-purple-500/15 text-purple-400">
                        NOMINATE
                      </span>
                    )}
                  </div>

                  {/* Auction note */}
                  {player.auction_note && (
                    <p className="text-xs text-slate-400 leading-relaxed mb-2">
                      {player.auction_note}
                    </p>
                  )}

                  {/* Signal disagreement context */}
                  {player.value_gap_signal && player.value_assessment && (() => {
                    const sig = player.value_gap_signal
                    const assess = player.value_assessment
                    const agree = (
                      (sig === 'market_undervalues' && ['elite_value', 'good_value'].includes(assess)) ||
                      (sig === 'market_overvalues' && ['avoid', 'slight_overpay'].includes(assess)) ||
                      (sig === 'aligned' && assess === 'fair_value') ||
                      sig === 'no_market_data' || sig === 'no_system_data'
                    )
                    if (agree) return null
                    const explanation = (
                      sig === 'market_overvalues' && ['elite_value', 'good_value'].includes(assess)
                        ? 'AI sees upside the historical average misses.'
                        : sig === 'market_undervalues' && ['avoid', 'slight_overpay'].includes(assess)
                          ? 'AI applying contextual discount to math value.'
                          : null
                    )
                    return (
                      <div className="flex items-start gap-1.5 mt-1">
                        <Info size={13} className="text-slate-500 mt-0.5 shrink-0" />
                        <span className="text-[11px] text-slate-500 leading-snug">
                          Math signal ({sig.replace(/_/g, ' ')}) differs from AI — AI wins for bid decisions.
                          {explanation && ` ${explanation}`}
                        </span>
                      </div>
                    )
                  })()}
                </div>
              )}

              <ValueComparisonBar
                systemValue={player.baseline_value}
                marketValue={player.market_value}
              />
              </>
              )}
            </Section>

            {/* Projection */}
            {(player.profile?.clean_season_baseline?.projected_ppr_season || player.profile?.clean_season_baseline?.ppr_points) && (
              <Section title="Projection">
                {(() => {
                  const baseline = player.profile.clean_season_baseline || {}
                  return (<>
                {/* Source badge */}
                <div className="flex items-center gap-2 mb-3">
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${
                    player.profile.profile_source === 'sonnet_projection'
                      ? 'bg-purple-500/15 text-purple-400'
                      : player.profile.profile_source === 'sonnet_rookie'
                        ? 'bg-cyan-500/15 text-cyan-400'
                        : player.profile.profile_source === 'college_comps'
                          ? 'bg-amber-500/15 text-amber-400'
                          : 'bg-slate-500/15 text-slate-400'
                  }`}>
                    {player.profile.profile_source === 'sonnet_projection' ? 'AI Projection'
                      : player.profile.profile_source === 'sonnet_rookie' ? 'Rookie AI Projection'
                      : player.profile.profile_source === 'college_comps' ? 'College Comp Profile'
                      : 'Historical'}
                  </span>
                  {player.ai_bid_ceiling != null && player.profile.profile_source === 'college_comps' && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-purple-500/15 text-purple-400">
                      AI Calibrated
                    </span>
                  )}
                  {player.profile.confidence && (
                    <span className={`text-[10px] ${
                      player.profile.confidence === 'low' ? 'text-amber-400' : 'text-slate-500'
                    }`}>
                      {player.profile.confidence === 'low' ? 'Low' : player.profile.confidence === 'medium' ? 'Medium' : player.profile.confidence.charAt(0).toUpperCase() + player.profile.confidence.slice(1)} Confidence
                    </span>
                  )}
                </div>

                {/* PPR total */}
                {(() => {
                  const projectedPPR = baseline.projected_ppr_season ?? baseline.ppr_points
                  const isSonnet = player.profile.profile_source === 'sonnet_projection'
                  const isSonnetRookie = player.profile.profile_source === 'sonnet_rookie'
                  const isCollegeComps = player.profile.profile_source === 'college_comps'
                  const projCeiling = player.profile.ceiling_value_ppr
                    ?? baseline.upside_ppr
                    ?? projectedPPR * 1.25
                  const projFloor = player.profile.floor_value_ppr
                    ?? baseline.downside_ppr
                    ?? projectedPPR * 0.75
                  const validatedProjection = Math.min(Math.max(projectedPPR, projFloor), projCeiling)
                  if (validatedProjection !== projectedPPR) {
                    console.warn(
                      `${player.name}: projection ${projectedPPR} outside range [${projFloor}, ${projCeiling}]`
                    )
                  }
                  return (
                    <div className="bg-[#1c1f2e] rounded p-3 mb-3">
                      <div className="text-[10px] text-slate-500 mb-1">
                        {(isSonnet || isSonnetRookie) ? 'Projected PPR (17 games)' : 'Baseline PPR (17 games)'}
                      </div>
                      <div className="text-xl font-mono font-semibold text-blue-400">
                        {projectedPPR?.toFixed(1)}
                      </div>
                      {(isSonnet || isSonnetRookie || isCollegeComps) && projCeiling > 0 && (
                        <div className="mt-2">
                          <div className="flex justify-between text-[10px] text-slate-500 mb-1">
                            <span>Floor: {projFloor?.toFixed(0)}</span>
                            <span>Ceiling: {projCeiling?.toFixed(0)}</span>
                          </div>
                          <div className="relative h-2 bg-[#161822] rounded-full overflow-hidden">
                            <div className="absolute h-full bg-blue-500/20 rounded-full"
                              style={{
                                left: `${(projFloor / projCeiling) * 100 * 0.9}%`,
                                right: '0%'
                              }}
                            />
                            <div className="absolute h-full w-1 bg-blue-400 rounded-full"
                              style={{
                                left: `${(validatedProjection / projCeiling) * 100 * 0.9}%`
                              }}
                            />
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })()}

                {/* Reasoning — for AI projections and rookie Sonnet */}
                {(player.profile.profile_source === 'sonnet_projection' || player.profile.profile_source === 'sonnet_rookie') && player.profile.projection_reasoning && (
                  <p className="text-xs text-slate-400 leading-relaxed mb-3">
                    {player.profile.projection_reasoning}
                  </p>
                )}

                {/* College comps without Sonnet — show insufficient data note */}
                {player.profile.profile_source === 'college_comps' && !baseline.projected_ppr_season && (
                  <p className="text-xs text-slate-500 italic mb-3">
                    Insufficient data for AI projection
                  </p>
                )}

                {/* Rookie AI metrics — breakout probability, comp grade, risks/upside */}
                {player.profile.profile_source === 'sonnet_rookie' && (
                  <div className="space-y-2 mb-3">
                    <div className="grid grid-cols-2 gap-2">
                      {baseline.breakout_probability != null && (
                        <Stat label="Breakout Probability" value={`${Math.round(baseline.breakout_probability * 100)}%`} />
                      )}
                      {baseline.comp_translation_grade && (
                        <Stat label="Comp Grade" value={baseline.comp_translation_grade} />
                      )}
                    </div>
                    {baseline.key_risks?.length > 0 && (
                      <div>
                        <div className="text-[10px] text-slate-500 mb-1">Key Risks</div>
                        <ul className="text-xs text-red-400/80 space-y-0.5">
                          {baseline.key_risks.map((risk, i) => (
                            <li key={i} className="flex gap-1"><span>-</span><span>{risk}</span></li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {baseline.key_upside_factors?.length > 0 && (
                      <div>
                        <div className="text-[10px] text-slate-500 mb-1">Upside Factors</div>
                        <ul className="text-xs text-emerald-400/80 space-y-0.5">
                          {baseline.key_upside_factors.map((factor, i) => (
                            <li key={i} className="flex gap-1"><span>-</span><span>{factor}</span></li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                )}

                {/* Career trajectory + key metrics — for AI projections */}
                {player.profile.profile_source === 'sonnet_projection' && (
                  <div className="grid grid-cols-2 gap-2">
                    {player.profile.career_trajectory && (
                      <Stat label="Trajectory" value={player.profile.career_trajectory} />
                    )}
                    {player.profile.separation_score && (
                      <Stat label="Separation" value={player.profile.separation_score} />
                    )}
                    {player.profile.yards_after_catch_score && (
                      <Stat label="YAC" value={player.profile.yards_after_catch_score} />
                    )}
                  </div>
                )}
                  </>)
                })()}
              </Section>
            )}

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
                          Trigger:{' '}
                          {dep.trigger_player_id ? (
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                openPlayerDetail(dep.trigger_player_id)
                              }}
                              className="text-blue-400 hover:underline"
                            >
                              {dep.trigger_player_name}
                            </button>
                          ) : (
                            dep.trigger_player_name
                          )}
                        </div>
                      )}
                      {dep.reasoning && (
                        <div className="text-xs text-slate-500 mt-1">{dep.reasoning}</div>
                      )}
                      {dep.value_impact_pct != null && (() => {
                        const v = dep.value_impact_pct
                        const display = Math.abs(v) >= 1 ? Math.round(v) : (Math.abs(v) < 0.01 ? 0 : +v.toFixed(1))
                        return (
                          <div className={`text-xs mt-1 ${display > 0 ? 'text-emerald-400' : display < 0 ? 'text-red-400' : 'text-slate-500'}`}>
                            Impact: {display > 0 ? '+' : ''}{display}%
                          </div>
                        )
                      })()}
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
