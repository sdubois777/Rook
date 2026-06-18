import { useState, useMemo, useRef, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '@clerk/clerk-react'
import { Star, StarOff, Download, Printer, Search, X, ChevronDown } from 'lucide-react'
import { fetchDraftboard } from '../api/draftboard'
import { usePreferencesStore } from '../stores/preferences'
import { useUIStore } from '../stores/ui'
import { useLeague } from '../context/LeagueContext'
import PositionBadge from '../components/shared/PositionBadge'
import SortableHeader from '../components/shared/SortableHeader'
import FilterBar, { FilterSelect } from '../components/shared/FilterBar'
import PlayerDetailPanel from '../components/PlayerDetailPanel'
import { buildPositionOptions } from '../lib/constants'
import {
  getBidCeiling,
  getDisplayAdp,
  getFpAdp,
  getAdpDiff,
  formatAdp,
  formatFpAdp,
  formatAdpDiff,
  getSnakeFlagClass,
  getSnakeFlagLabel,
} from '../utils/playerUtils'

const STRATEGY_OPTIONS = [
  { value: '', label: 'No Strategy' },
  { value: 'hero_rb', label: 'Hero RB' },
  { value: 'zero_rb', label: 'Zero RB' },
  { value: 'stars_and_scrubs', label: 'Stars & Scrubs' },
  { value: 'balanced', label: 'Balanced' },
]

const POSITION_OPTIONS = buildPositionOptions('All Positions')

const NFL_TEAMS = [
  'ARI','ATL','BAL','BUF','CAR','CHI','CIN','CLE',
  'DAL','DEN','DET','GB','HOU','IND','JAX','KC',
  'LA','LAC','LV','MIA','MIN','NE','NO','NYG',
  'NYJ','PHI','PIT','SEA','SF','TB','TEN','WAS',
]

const TEAM_OPTIONS = [
  { value: '', label: 'All Teams' },
  ...NFL_TEAMS.map((t) => ({ value: t, label: t })),
]

const FLAG_OPTIONS = ['PAY UP', 'NOMINATE', 'AVOID', 'ROOKIE', 'BREAKOUT']

/** Returns the set of badge labels a player has on the draft board. */
function getPlayerBadges(p) {
  const badges = []
  if (p.pay_up_flag) badges.push('PAY UP')
  if (p.breakout_flag) badges.push('BREAKOUT')
  if (p.nomination_target_flag) badges.push('NOMINATE')
  if (p.is_rookie) badges.push('ROOKIE')
  if (p.value_assessment && ['avoid', 'strong_avoid'].includes(p.value_assessment)) badges.push('AVOID')
  return badges
}

function getPlayerGap(p) {
  const ceiling = getBidCeiling(p)
  return ceiling != null && p.market_value != null ? ceiling - p.market_value : null
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
      case 'ai_ceiling': va = getBidCeiling(a) ?? -Infinity; vb = getBidCeiling(b) ?? -Infinity; break
      case 'system': va = a.baseline_value ?? -Infinity; vb = b.baseline_value ?? -Infinity; break
      case 'market': va = a.market_value ?? -Infinity; vb = b.market_value ?? -Infinity; break
      case 'ppr': va = a.ppr_points ?? -Infinity; vb = b.ppr_points ?? -Infinity; break
      case 'gap': va = getPlayerGap(a) ?? -Infinity; vb = getPlayerGap(b) ?? -Infinity; break
      // Snake: nulls always sort last regardless of direction.
      case 'adp_rank': va = getDisplayAdp(a) ?? Infinity; vb = getDisplayAdp(b) ?? Infinity; break
      case 'adp_fantasypros': va = getFpAdp(a) ?? Infinity; vb = getFpAdp(b) ?? Infinity; break
      case 'adp_diff': va = getAdpDiff(a) ?? -Infinity; vb = getAdpDiff(b) ?? -Infinity; break
      default: va = a.tier ?? 99; vb = b.tier ?? 99; break
    }
    if (typeof va === 'string') return va < vb ? -dir : va > vb ? dir : 0
    return (va - vb) * dir
  })
  return sorted
}

function FlagsDropdown({ selected, onChange }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    function handleClickOutside(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const toggle = (flag) => {
    if (selected.includes(flag)) {
      onChange(selected.filter((f) => f !== flag))
    } else {
      onChange([...selected, flag])
    }
  }

  const label = selected.length > 0 ? `Flags (${selected.length})` : 'Flags'

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className={`flex items-center gap-1.5 text-sm border rounded px-2 py-1 transition-colors ${
          selected.length > 0
            ? 'text-blue-400 border-blue-500/50 bg-blue-500/10'
            : 'text-slate-300 border-[#2d3148] bg-[#1c1f2e]'
        }`}
      >
        {label}
        <ChevronDown size={13} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="absolute top-full left-0 mt-1 z-50 bg-[#1c1f2e] border border-[#2d3148] rounded shadow-lg py-1 min-w-[150px]">
          {FLAG_OPTIONS.map((flag) => (
            <label
              key={flag}
              className="flex items-center gap-2 px-3 py-1.5 text-sm text-slate-300 hover:bg-[#222539] cursor-pointer"
            >
              <input
                type="checkbox"
                checked={selected.includes(flag)}
                onChange={() => toggle(flag)}
                className="rounded border-[#2d3148] bg-[#161822] text-blue-500 focus:ring-blue-500/30"
              />
              {flag}
            </label>
          ))}
          {selected.length > 0 && (
            <button
              onClick={() => onChange([])}
              className="w-full text-left px-3 py-1.5 text-xs text-slate-500 hover:text-slate-300 border-t border-[#2d3148] mt-1"
            >
              Clear all
            </button>
          )}
        </div>
      )}
    </div>
  )
}

const SNAKE_SORT_KEYS = ['adp_rank', 'adp_fantasypros', 'adp_diff']

export default function DraftBoard() {
  const { isSnake, selectedLeague } = useLeague()
  // Floor at 8: a snake league with fewer than 8 teams isn't a real league. The
  // test league has team_count=1, which would otherwise put every player in
  // their own round (round = ceil(adp_rank / teamCount)).
  const teamCount = Math.max(selectedLeague?.team_count || 12, 8)
  const { isLoaded } = useAuth()
  const [strategy, setStrategy] = useState('')
  const [position, setPosition] = useState('')
  const [team, setTeam] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [selectedFlags, setSelectedFlags] = useState([])
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
    // Don't fetch until Clerk is ready, or the request goes out tokenless -> 401.
    enabled: isLoaded,
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

  // Apply all client-side filters + sort
  const sortedPlayers = useMemo(() => {
    let filtered = allPlayers

    // Search filter
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase()
      filtered = filtered.filter((p) => p.name.toLowerCase().includes(q))
    }

    // Team filter
    if (team) {
      filtered = filtered.filter((p) => p.team_abbr === team)
    }

    // Flags filter (OR logic — player must have ANY of the selected badges)
    if (selectedFlags.length > 0) {
      filtered = filtered.filter((p) => {
        const badges = getPlayerBadges(p)
        return selectedFlags.some((f) => badges.includes(f))
      })
    }

    // Watchlist filter
    if (showWatchlistOnly) {
      filtered = filtered.filter((p) => isWatchlisted(p.id))
    }

    if (isSnake) {
      // Snake: sort by a snake column if one is selected, else default to
      // adp_rank ascending (the draft order). Nulls sort last via sortPlayers.
      const snakeKey = SNAKE_SORT_KEYS.includes(sortKey) ? sortKey : 'adp_rank'
      const snakeDir = SNAKE_SORT_KEYS.includes(sortKey) ? sortOrder : 'asc'
      return sortPlayers(filtered, snakeKey, snakeDir)
    }
    return sortPlayers(filtered, sortKey, sortOrder)
  }, [allPlayers, searchQuery, team, selectedFlags, showWatchlistOnly, watchlist, sortKey, sortOrder, isSnake])

  // Group by round when snake + sorted by adp_rank (the draft order); group by
  // tier when auction + sorted by tier; otherwise a flat list.
  const isRoundGroup = isSnake && (!SNAKE_SORT_KEYS.includes(sortKey) || sortKey === 'adp_rank')
  const isTierSort = !isSnake && sortKey === 'tier'

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
        const aiCeiling = getBidCeiling(p)
        const ceiling = aiCeiling ?? p.recommended_bid_ceiling?.toFixed(0) ?? '--'
        const market = p.market_value?.toFixed(0) ?? '--'
        const gap = aiCeiling != null && p.market_value != null
          ? (aiCeiling - p.market_value > 0 ? '+' : '') + (aiCeiling - p.market_value).toFixed(0)
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

          {isSnake ? (
            <>
              {/* Snake: AI ADP (clean adp_rank) / FP ADP / Diff (fp_rank -
                  adp_rank; positive = we rate them earlier than consensus). */}
              <span className="text-sm text-purple-400 font-mono w-20 shrink-0 text-right">
                {formatAdp(p)}
              </span>
              <span className="text-xs text-slate-400 font-mono w-20 shrink-0 text-right">
                {formatFpAdp(p)}
              </span>
              <span
                className={`text-xs font-mono w-16 shrink-0 text-right ${
                  getAdpDiff(p) != null && getAdpDiff(p) > 3
                    ? 'text-emerald-400'
                    : getAdpDiff(p) != null && getAdpDiff(p) < -3
                    ? 'text-red-400'
                    : 'text-slate-500'
                }`}
              >
                {formatAdpDiff(p)}
              </span>
            </>
          ) : (
            <>
              <span className="text-sm text-purple-400 font-mono w-20 shrink-0 text-right">
                {getBidCeiling(p) != null ? `$${getBidCeiling(p)}` : '--'}
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
            </>
          )}

          {/* Flags — snake shows the snake_flag; auction shows the $ badges */}
          <div className="flex gap-1 ml-auto flex-wrap justify-end">
            {isSnake ? (
              getSnakeFlagLabel(p) && (
                <span
                  className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${getSnakeFlagClass(p)}`}
                >
                  {getSnakeFlagLabel(p)}
                </span>
              )
            ) : (
              <>
                {p.pay_up_flag && (
                  <span className="text-[10px] text-emerald-400 bg-emerald-500/15 px-1.5 py-0.5 rounded-full font-medium">
                    PAY UP
                  </span>
                )}
                {p.breakout_flag && (
                  <span className="text-[10px] text-yellow-400 bg-yellow-500/15 px-1.5 py-0.5 rounded-full font-medium">
                    Breakout
                  </span>
                )}
                {p.nomination_target_flag && (
                  <span className="text-[10px] text-purple-400 bg-purple-500/15 px-1.5 py-0.5 rounded-full font-medium">
                    NOMINATE
                  </span>
                )}
                {p.is_rookie && (
                  <span className="text-[10px] text-cyan-400 bg-cyan-500/15 px-1.5 py-0.5 rounded-full font-medium">
                    Rookie
                  </span>
                )}
                {p.value_assessment && ['avoid', 'strong_avoid'].includes(p.value_assessment) && (
                  <span className="text-[10px] text-red-400 bg-red-500/15 px-1.5 py-0.5 rounded-full font-medium">
                    Avoid
                  </span>
                )}
              </>
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
        {isSnake ? (
          <>
            <SortableHeader label="AI ADP" sortKey="adp_rank" currentSort={sortKey} currentOrder={sortOrder} onSort={handleSort} className="w-20 shrink-0" align="right" defaultOrder="asc" />
            <SortableHeader label="FP ADP" sortKey="adp_fantasypros" currentSort={sortKey} currentOrder={sortOrder} onSort={handleSort} className="w-20 shrink-0" align="right" defaultOrder="asc" />
            <SortableHeader label="Diff" sortKey="adp_diff" currentSort={sortKey} currentOrder={sortOrder} onSort={handleSort} className="w-16 shrink-0" align="right" defaultOrder="desc" />
          </>
        ) : (
          <>
            <SortableHeader label="AI Ceil" sortKey="ai_ceiling" currentSort={sortKey} currentOrder={sortOrder} onSort={handleSort} className="w-20 shrink-0" align="right" />
            <SortableHeader label="ADP" sortKey="market" currentSort={sortKey} currentOrder={sortOrder} onSort={handleSort} className="w-20 shrink-0" align="right" />
            <SortableHeader label="PPR" sortKey="ppr" currentSort={sortKey} currentOrder={sortOrder} onSort={handleSort} className="w-20 shrink-0" align="right" />
            <SortableHeader label="Gap" sortKey="gap" currentSort={sortKey} currentOrder={sortOrder} onSort={handleSort} className="w-16 shrink-0" align="right" />
          </>
        )}
        <span className="ml-auto text-[10px] uppercase tracking-wider text-slate-500">Flags</span>
      </div>
    </div>
  )

  // Grouped view: by round (snake) or tier (auction). Round size = team_count.
  const tierGroups = useMemo(() => {
    if (!isTierSort && !isRoundGroup) return null
    const groups = {}
    for (const p of sortedPlayers) {
      const rank = getDisplayAdp(p)
      const key = isRoundGroup
        ? String(rank != null ? Math.floor((rank - 1) / teamCount) + 1 : 0)
        : String(p.tier ?? 0)
      if (!groups[key]) groups[key] = []
      groups[key].push(p)
    }
    return groups
  }, [sortedPlayers, isTierSort, isRoundGroup, teamCount])

  const groupLabel = isRoundGroup ? 'Round' : 'Tier'

  return (
    <div className="max-w-6xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-semibold text-slate-100 print-full-width">Draft Board</h1>
        <div className="flex items-center gap-3 no-print">
          <span className="text-sm text-slate-500">{sortedPlayers.length} of {totalPlayers} players</span>
          <button onClick={handleExportTxt} className="flex items-center gap-1 px-2.5 py-1.5 text-xs bg-[#1c1f2e] text-slate-300 border border-[#2d3148] rounded hover:bg-[#222539] transition-colors" title="Export TXT cheat sheet">
            <Download size={13} /> Export
          </button>
          <button onClick={handlePrint} className="flex items-center gap-1 px-2.5 py-1.5 text-xs bg-[#1c1f2e] text-slate-300 border border-[#2d3148] rounded hover:bg-[#222539] transition-colors" title="Print draft board">
            <Printer size={13} /> Print
          </button>
        </div>
      </div>

      {/* Budget bar — auction only. Snake has no budget. */}
      {!isSnake && (
        <div className="flex items-center gap-4 bg-[#161822] rounded-lg border border-[#2d3148] px-4 py-2.5 mb-3 text-sm no-print">
          <span className="text-slate-200 font-medium">Budget: $200</span>
          <span className="text-slate-400">Skill starters: <span className="text-blue-400 font-mono">$185</span></span>
          <span className="text-slate-500 text-xs">(Bench + K + DEF: $15)</span>
        </div>
      )}


      <FilterBar>
        {/* Search */}
        <div className="relative">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search players..."
            className="w-48 pl-8 pr-8 py-1 text-sm bg-[#1c1f2e] text-slate-300 border border-[#2d3148] rounded focus:outline-none focus:border-blue-500/50 placeholder-slate-600"
          />
          {searchQuery && (
            <button
              onClick={() => setSearchQuery('')}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
            >
              <X size={14} />
            </button>
          )}
        </div>

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
        <FilterSelect
          label="Team"
          value={team}
          onChange={setTeam}
          options={TEAM_OPTIONS}
        />
        <FlagsDropdown selected={selectedFlags} onChange={setSelectedFlags} />
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
      ) : (isTierSort || isRoundGroup) && tierGroups ? (
        /* Grouped view — rounds (snake) or tiers (auction) */
        <div className="space-y-4">
          {Object.keys(tierGroups).sort((a, b) => {
            // Rounds always ascending; tiers respect the sort direction.
            const dir = isRoundGroup ? 1 : sortOrder === 'asc' ? 1 : -1
            return (parseInt(a) - parseInt(b)) * dir
          }).map((tierKey) => {
            const players = tierGroups[tierKey]
            if (!players || players.length === 0) return null
            return (
              <div key={tierKey} className="bg-[#161822] rounded-lg border border-[#2d3148] overflow-hidden">
                <div className="px-4 py-2.5 border-b border-[#2d3148] flex items-center justify-between">
                  <h3 className="text-sm font-medium text-slate-200">
                    {groupLabel} {tierKey}
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
