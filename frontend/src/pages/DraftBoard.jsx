import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Star, StarOff, Download, Printer } from 'lucide-react'
import { fetchDraftboard } from '../api/draftboard'
import { fetchMarketValueStatus } from '../api/admin'
import { usePreferencesStore } from '../stores/preferences'
import { useUIStore } from '../stores/ui'
import PositionBadge from '../components/shared/PositionBadge'
import FlagBadge from '../components/shared/FlagBadge'
import SortableHeader from '../components/shared/SortableHeader'
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

function getPlayerGap(p) {
  return p.ai_bid_ceiling != null && p.market_value != null
    ? p.ai_bid_ceiling - p.market_value
    : null
}

function sortPlayers(players, sortKey, sortOrder) {
  const sorted = [...players]
  const dir = sortOrder === 'asc' ? 1 : -1

  sorted.sort((a, b) => {
    let va, vb
    switch (sortKey) {
      case 'tier': va = a.tier ?? 99; vb = b.tier ?? 99; break
      case 'name': va = a.name?.toLowerCase() ?? ''; vb = b.name?.toLowerCase() ?? ''; break
      case 'ceiling': va = a.recommended_bid_ceiling ?? -Infinity; vb = b.recommended_bid_ceiling ?? -Infinity; break
      case 'ai_ceiling': va = a.ai_bid_ceiling ?? -Infinity; vb = b.ai_bid_ceiling ?? -Infinity; break
      case 'system': va = a.baseline_value ?? -Infinity; vb = b.baseline_value ?? -Infinity; break
      case 'market': va = a.market_value ?? -Infinity; vb = b.market_value ?? -Infinity; break
      case 'ppr': va = a.ppr_points ?? -Infinity; vb = b.ppr_points ?? -Infinity; break
      case 'gap': va = getPlayerGap(a) ?? -Infinity; vb = getPlayerGap(b) ?? -Infinity; break
      default: va = a.tier ?? 99; vb = b.tier ?? 99; break
    }
    if (typeof va === 'string') return va < vb ? -dir : va > vb ? dir : 0
    return (va - vb) * dir
  })
  return sorted
}

export default function DraftBoard() {
  const [strategy, setStrategy] = useState('')
  const [position, setPosition] = useState('')
  const [showWatchlistOnly, setShowWatchlistOnly] = useState(false)
  const [sortKey, setSortKey] = useState('tier')
  const [sortOrder, setSortOrder] = useState('asc')

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

  // Flatten all players from all tiers
  const allPlayers = useMemo(() => {
    const flat = []
    for (const key of tierKeys) {
      for (const p of tiers[key] || []) flat.push(p)
    }
    return flat
  }, [tiers, tierKeys])

  // Apply watchlist filter + sort
  const sortedPlayers = useMemo(() => {
    let filtered = allPlayers
    if (showWatchlistOnly) {
      filtered = filtered.filter((p) => isWatchlisted(p.id))
    }
    return sortPlayers(filtered, sortKey, sortOrder)
  }, [allPlayers, showWatchlistOnly, watchlist, sortKey, sortOrder])

  const isTierSort = sortKey === 'tier'

  const handleSort = (key, order) => {
    setSortKey(key)
    setSortOrder(order)
  }

  const handleStrategyChange = (v) => {
    setStrategy(v)
    if (v) setGlobalStrategy(v).catch(() => {})
  }

  const handleExportTxt = () => {
    const lines = ['DRAFT CHEAT SHEET', `Strategy: ${strategy || 'None'}`, '']
    for (const tierKey of tierKeys) {
      const players = tiers[tierKey] || []
      lines.push(`--- TIER ${tierKey} ---`)
      for (const p of players) {
        const ceiling = p.ai_bid_ceiling ?? p.recommended_bid_ceiling?.toFixed(0) ?? '--'
        const market = p.market_value?.toFixed(0) ?? '--'
        const gap = p.ai_bid_ceiling != null && p.market_value != null
          ? (p.ai_bid_ceiling - p.market_value > 0 ? '+' : '') + (p.ai_bid_ceiling - p.market_value).toFixed(0)
          : '--'
        lines.push(`${p.position.padEnd(3)} ${p.name.padEnd(22)} ${p.team_abbr.padEnd(5)} Ceil:$${ceiling.toString().padStart(3)}  Mkt:$${market.toString().padStart(3)}  Gap:${gap}`)
      }
      lines.push('')
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'draft-cheat-sheet.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  const handlePrint = () => window.print()

  const renderPlayerRow = (p) => {
    const highlight = p.strategy_highlight
    const watched = isWatchlisted(p.id)
    const aiGap = getPlayerGap(p)

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
          <span className="w-9 shrink-0"><PositionBadge position={p.position} /></span>
          <span className="text-sm font-medium text-slate-200 w-[220px] shrink-0 truncate">
            {p.name}
          </span>
          <span className="text-xs text-slate-500 w-12 shrink-0">{p.team_abbr}</span>

          <span className="text-sm text-purple-400 font-mono w-20 shrink-0 text-right">
            {p.ai_bid_ceiling != null ? `$${p.ai_bid_ceiling}` : '--'}
          </span>
          <span className="text-xs text-slate-400 font-mono w-20 shrink-0 text-right">
            ${p.market_value?.toFixed(0) || '--'}
          </span>
          <span className="text-xs text-slate-400 font-mono w-20 shrink-0 text-right">
            {p.ppr_points ? `${p.ppr_points.toFixed(0)} PPR` : ''}
          </span>

          <span
            className={`text-xs font-mono w-16 shrink-0 text-right ${
              aiGap != null && aiGap > 3
                ? 'text-emerald-400'
                : aiGap != null && aiGap < -3
                ? 'text-red-400'
                : 'text-slate-500'
            }`}
          >
            {aiGap != null
              ? `${aiGap > 0 ? '+' : ''}${aiGap.toFixed(0)}`
              : '--'}
          </span>

          {/* Flags */}
          <div className="flex gap-1 ml-auto flex-wrap justify-end">
            {p.is_rookie && (
              <span className="text-[10px] text-cyan-400 bg-cyan-500/15 px-1.5 py-0.5 rounded-full font-medium">
                Rookie
              </span>
            )}
            {p.pay_up_flag && (
              <span className="text-[10px] text-emerald-400 bg-emerald-500/15 px-1.5 py-0.5 rounded-full font-medium">
                PAY UP
              </span>
            )}
            {p.nomination_target_flag && (
              <span className="text-[10px] text-purple-400 bg-purple-500/15 px-1.5 py-0.5 rounded-full font-medium">
                NOMINATE
              </span>
            )}
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
  }

  const columnHeaders = (
    <div className="flex items-center gap-3 px-4 py-1.5 border-b border-[#2d3148]">
      <span className="w-[14px] shrink-0" />
      <div className="flex items-center gap-3 flex-1 min-w-0">
        <span className="w-9 shrink-0 text-[10px] uppercase tracking-wider text-slate-500">Pos</span>
        <SortableHeader label="Player" sortKey="name" currentSort={sortKey} currentOrder={sortOrder} onSort={handleSort} className="w-[220px] shrink-0" defaultOrder="asc" />
        <span className="w-12 shrink-0 text-[10px] uppercase tracking-wider text-slate-500">Team</span>
        <SortableHeader label="AI Ceil" sortKey="ai_ceiling" currentSort={sortKey} currentOrder={sortOrder} onSort={handleSort} className="w-20 shrink-0" align="right" />
        <SortableHeader label="Market" sortKey="market" currentSort={sortKey} currentOrder={sortOrder} onSort={handleSort} className="w-20 shrink-0" align="right" />
        <SortableHeader label="PPR" sortKey="ppr" currentSort={sortKey} currentOrder={sortOrder} onSort={handleSort} className="w-20 shrink-0" align="right" />
        <SortableHeader label="Gap" sortKey="gap" currentSort={sortKey} currentOrder={sortOrder} onSort={handleSort} className="w-16 shrink-0" align="right" />
        <span className="ml-auto text-[10px] uppercase tracking-wider text-slate-500">Flags</span>
      </div>
    </div>
  )

  // Group sorted players by tier for tier-grouped view
  const tierGroups = useMemo(() => {
    if (!isTierSort) return null
    const groups = {}
    for (const p of sortedPlayers) {
      const key = String(p.tier ?? 0)
      if (!groups[key]) groups[key] = []
      groups[key].push(p)
    }
    return groups
  }, [sortedPlayers, isTierSort])

  return (
    <div className="max-w-6xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-semibold text-slate-100 print-full-width">Draft Board</h1>
        <div className="flex items-center gap-3 no-print">
          <span className="text-sm text-slate-500">{totalPlayers} players</span>
          <button onClick={handleExportTxt} className="flex items-center gap-1 px-2.5 py-1.5 text-xs bg-[#1c1f2e] text-slate-300 border border-[#2d3148] rounded hover:bg-[#222539] transition-colors" title="Export TXT cheat sheet">
            <Download size={13} /> Export
          </button>
          <button onClick={handlePrint} className="flex items-center gap-1 px-2.5 py-1.5 text-xs bg-[#1c1f2e] text-slate-300 border border-[#2d3148] rounded hover:bg-[#222539] transition-colors" title="Print draft board">
            <Printer size={13} /> Print
          </button>
        </div>
      </div>

      {/* Budget bar */}
      <div className="flex items-center gap-4 bg-[#161822] rounded-lg border border-[#2d3148] px-4 py-2.5 mb-3 text-sm no-print">
        <span className="text-slate-200 font-medium">Budget: $200</span>
        <span className="text-slate-400">Skill starters: <span className="text-blue-400 font-mono">$185</span></span>
        <span className="text-slate-500 text-xs">(Bench + K + DEF: $15)</span>
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
        {sortKey !== 'tier' && (
          <button
            onClick={() => { setSortKey('tier'); setSortOrder('asc') }}
            className="text-xs text-blue-400 hover:text-blue-300 transition-colors"
          >
            Reset to tier view
          </button>
        )}
      </FilterBar>

      {isLoading ? (
        <div className="py-20 text-center text-slate-500">Loading draft board...</div>
      ) : sortedPlayers.length === 0 ? (
        <div className="py-20 text-center text-slate-500">No ranked players found.</div>
      ) : isTierSort && tierGroups ? (
        /* Tier-grouped view (default) */
        <div className="space-y-4">
          {Object.keys(tierGroups).sort((a, b) => {
            const dir = sortOrder === 'asc' ? 1 : -1
            return (parseInt(a) - parseInt(b)) * dir
          }).map((tierKey) => {
            const players = tierGroups[tierKey]
            if (!players || players.length === 0) return null
            return (
              <div key={tierKey} className="bg-[#161822] rounded-lg border border-[#2d3148] overflow-hidden">
                <div className="px-4 py-2.5 border-b border-[#2d3148] flex items-center justify-between">
                  <h3 className="text-sm font-medium text-slate-200">
                    Tier {tierKey}
                  </h3>
                  <span className="text-xs text-slate-500">{players.length} players</span>
                </div>
                {columnHeaders}
                {players.map(renderPlayerRow)}
              </div>
            )
          })}
        </div>
      ) : (
        /* Flat sorted view */
        <div className="bg-[#161822] rounded-lg border border-[#2d3148] overflow-hidden">
          {columnHeaders}
          {sortedPlayers.map(renderPlayerRow)}
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
