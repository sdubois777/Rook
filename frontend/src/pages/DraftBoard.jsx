import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Star, StarOff } from 'lucide-react'
import { fetchDraftboard } from '../api/draftboard'
import { fetchMarketValueStatus } from '../api/admin'
import { usePreferencesStore } from '../stores/preferences'
import { useUIStore } from '../stores/ui'
import PositionBadge from '../components/shared/PositionBadge'
import FlagBadge from '../components/shared/FlagBadge'
import FilterBar, { FilterSelect } from '../components/shared/FilterBar'
import PlayerDetailPanel from '../components/PlayerDetailPanel'

const STRATEGY_OPTIONS = [
  { value: '', label: 'No Strategy' },
  { value: 'hero_rb', label: 'Hero RB' },
  { value: 'zero_rb', label: 'Zero RB' },
  { value: 'stars_and_scrubs', label: 'Stars & Scrubs' },
  { value: 'balanced', label: 'Balanced' },
]

const POSITION_OPTIONS = [
  { value: '', label: 'All Positions' },
  { value: 'QB', label: 'QB' },
  { value: 'RB', label: 'RB' },
  { value: 'WR', label: 'WR' },
  { value: 'TE', label: 'TE' },
]

export default function DraftBoard() {
  const [strategy, setStrategy] = useState('')
  const [position, setPosition] = useState('')
  const [showWatchlistOnly, setShowWatchlistOnly] = useState(false)

  const openPlayerDetail = useUIStore((s) => s.openPlayerDetail)
  const selectedPlayerId = useUIStore((s) => s.selectedPlayerId)
  const detailPanelOpen = useUIStore((s) => s.detailPanelOpen)
  const watchlist = usePreferencesStore((s) => s.watchlist)
  const addToWatchlist = usePreferencesStore((s) => s.addToWatchlist)
  const removeFromWatchlist = usePreferencesStore((s) => s.removeFromWatchlist)
  const isWatchlisted = (id) => watchlist.some((w) => w.player_id === id)
  const setGlobalStrategy = usePreferencesStore((s) => s.setStrategy)

  const { data, isLoading } = useQuery({
    queryKey: ['draftboard', strategy, position],
    queryFn: () =>
      fetchDraftboard({
        strategy: strategy || undefined,
        position: position || undefined,
      }),
  })

  const { data: marketStatus } = useQuery({
    queryKey: ['market-value-status'],
    queryFn: fetchMarketValueStatus,
    staleTime: 5 * 60 * 1000,
  })

  const tiers = data?.tiers || {}
  const tierKeys = Object.keys(tiers).sort((a, b) => parseInt(a) - parseInt(b))
  const totalPlayers = data?.total_players || 0

  const handleStrategyChange = (v) => {
    setStrategy(v)
    if (v) setGlobalStrategy(v).catch(() => {})
  }

  return (
    <div className="max-w-6xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-semibold text-slate-100">Draft Board</h1>
        <span className="text-sm text-slate-500">{totalPlayers} players</span>
      </div>

      {marketStatus?.year && (
        <div className={`text-xs px-3 py-1.5 rounded mb-3 ${
          marketStatus.is_current_season
            ? 'bg-emerald-900/30 text-emerald-400'
            : 'bg-amber-900/30 text-amber-400'
        }`}>
          {marketStatus.is_current_season
            ? `Using ${marketStatus.year} auction values — current season`
            : `Using ${marketStatus.year} auction values — refresh in July when ${marketStatus.year + 1} data is available`
          }
        </div>
      )}

      <FilterBar>
        <FilterSelect
          label="Strategy"
          value={strategy}
          onChange={handleStrategyChange}
          options={STRATEGY_OPTIONS}
        />
        <FilterSelect
          label="Position"
          value={position}
          onChange={setPosition}
          options={POSITION_OPTIONS}
        />
        <label className="flex items-center gap-2 text-xs text-slate-400 cursor-pointer">
          <input
            type="checkbox"
            checked={showWatchlistOnly}
            onChange={(e) => setShowWatchlistOnly(e.target.checked)}
            className="rounded border-[#2d3148] bg-[#1c1f2e] text-blue-500 focus:ring-blue-500/30"
          />
          Watchlist only
        </label>
      </FilterBar>

      {isLoading ? (
        <div className="py-20 text-center text-slate-500">Loading draft board...</div>
      ) : tierKeys.length === 0 ? (
        <div className="py-20 text-center text-slate-500">No ranked players found.</div>
      ) : (
        <div className="space-y-4">
          {tierKeys.map((tierKey) => {
            let players = tiers[tierKey] || []
            if (showWatchlistOnly) {
              players = players.filter((p) => isWatchlisted(p.id))
            }
            if (players.length === 0) return null

            return (
              <div key={tierKey} className="bg-[#161822] rounded-lg border border-[#2d3148] overflow-hidden">
                <div className="px-4 py-2.5 border-b border-[#2d3148] flex items-center justify-between">
                  <h3 className="text-sm font-medium text-slate-200">
                    Tier {tierKey}
                  </h3>
                  <span className="text-xs text-slate-500">{players.length} players</span>
                </div>

                {players.map((p) => {
                  const highlight = p.strategy_highlight
                  const watched = isWatchlisted(p.id)

                  let highlightClasses = ''
                  if (highlight === 'primary') {
                    highlightClasses = 'border-l-2 border-blue-500 bg-blue-500/5'
                  } else if (highlight === 'secondary') {
                    highlightClasses = 'border-l-2 border-purple-500 bg-purple-500/5'
                  } else if (highlight === 'dimmed') {
                    highlightClasses = 'opacity-40'
                  }

                  return (
                    <div
                      key={p.id}
                      className={`flex items-center gap-3 px-4 py-2.5 hover:bg-[#222539] cursor-pointer transition-colors border-b border-[#2d3148]/50 ${highlightClasses}`}
                    >
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          watched ? removeFromWatchlist(p.id) : addToWatchlist(p.id)
                        }}
                        className="shrink-0"
                      >
                        {watched ? (
                          <Star size={14} className="text-yellow-400 fill-yellow-400" />
                        ) : (
                          <StarOff size={14} className="text-slate-600 hover:text-slate-400" />
                        )}
                      </button>

                      <div
                        className="flex items-center gap-3 flex-1 min-w-0"
                        onClick={() => openPlayerDetail(p.id)}
                      >
                        <PositionBadge position={p.position} />
                        <span className="text-sm font-medium text-slate-200 min-w-[140px]">
                          {p.name}
                        </span>
                        <span className="text-xs text-slate-500 w-10">{p.team_abbr}</span>

                        <span className="text-sm text-blue-400 font-mono w-14 text-right">
                          ${p.recommended_bid_ceiling?.toFixed(0) || '--'}
                        </span>
                        <span className="text-xs text-slate-400 font-mono w-14 text-right">
                          ${p.baseline_value?.toFixed(0) || '--'}
                        </span>
                        <span className="text-xs text-slate-400 font-mono w-14 text-right">
                          ${p.market_value?.toFixed(0) || '--'}
                        </span>

                        {/* Value gap indicator */}
                        <span
                          className={`text-xs font-mono w-12 text-right ${
                            p.value_gap > 3
                              ? 'text-emerald-400'
                              : p.value_gap < -3
                              ? 'text-red-400'
                              : 'text-slate-500'
                          }`}
                        >
                          {p.value_gap != null
                            ? `${p.value_gap > 0 ? '+' : ''}${p.value_gap.toFixed(0)}`
                            : '--'}
                        </span>

                        {/* Flags */}
                        <div className="flex gap-1 ml-auto flex-wrap justify-end">
                          {(p.flags || []).slice(0, 2).map((f, i) => (
                            <FlagBadge key={i} flagType={f.flag_type} compact />
                          ))}
                          {p.breakout_flag && (
                            <span className="text-[10px] text-yellow-400 bg-yellow-500/15 px-1.5 py-0.5 rounded-full">
                              Breakout
                            </span>
                          )}
                          {p.injury_risk_level && p.injury_risk_level !== 'low' && (
                            <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${
                              p.injury_risk_level === 'high'
                                ? 'text-red-400 bg-red-500/15'
                                : 'text-amber-400 bg-amber-500/15'
                            }`}>
                              {p.injury_risk_level}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            )
          })}
        </div>
      )}

      {/* Strategy legend */}
      {strategy && (
        <div className="mt-4 flex items-center gap-4 text-xs text-slate-500">
          <span className="flex items-center gap-1">
            <span className="w-3 h-3 rounded border-l-2 border-blue-500 bg-blue-500/20" />
            Primary target
          </span>
          <span className="flex items-center gap-1">
            <span className="w-3 h-3 rounded border-l-2 border-purple-500 bg-purple-500/20" />
            Secondary target
          </span>
          <span className="flex items-center gap-1">
            <span className="w-3 h-3 rounded bg-slate-500/20 opacity-40" />
            De-prioritized
          </span>
        </div>
      )}

      {/* Detail panel */}
      {detailPanelOpen && selectedPlayerId && (
        <PlayerDetailPanel playerId={selectedPlayerId} />
      )}
    </div>
  )
}
