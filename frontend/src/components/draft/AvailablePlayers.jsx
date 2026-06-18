import { useMemo, useCallback } from 'react'
import { useDraftStore } from '../../stores/draft'
import { useLeague } from '../../context/LeagueContext'
import PositionBadge from '../shared/PositionBadge'
import SearchInput from '../shared/SearchInput'
import { FilterSelect } from '../shared/FilterBar'
import { buildPositionOptions } from '../../lib/constants'
import {
  getBidCeiling,
  getAdpDiff,
  formatAdp,
  formatFpAdp,
  formatAdpDiff,
  snakeSortComparator,
  auctionSortComparator,
} from '../../utils/playerUtils'

const POSITION_OPTIONS = buildPositionOptions('All')

export default function AvailablePlayers() {
  const availablePlayers = useDraftStore((s) => s.availablePlayers)
  const filter = useDraftStore((s) => s.availableFilter)
  const setFilter = useDraftStore((s) => s.setAvailableFilter)
  const { isSnake } = useLeague()

  const handleSearch = useCallback(
    (value) => setFilter({ search: value }),
    [setFilter]
  )

  const filtered = useMemo(() => {
    let list = availablePlayers

    if (filter.position) {
      list = list.filter((p) => p.position === filter.position)
    }

    if (filter.search) {
      const q = filter.search.toLowerCase()
      list = list.filter(
        (p) =>
          p.name?.toLowerCase().includes(q) ||
          p.team_abbr?.toLowerCase().includes(q)
      )
    }

    // Snake sorts by adp_rank ascending; auction by AI ceiling descending —
    // both via the shared comparators (single source of truth).
    return [...list].sort(isSnake ? snakeSortComparator : auctionSortComparator)
  }, [availablePlayers, filter, isSnake])

  return (
    <div className="h-full flex flex-col p-4">
      <div className="flex items-center gap-3 mb-3">
        <h3 className="text-sm font-medium text-slate-400 uppercase tracking-wider">
          Available
        </h3>
        <span className="text-xs text-slate-600">{filtered.length} players</span>
        <div className="ml-auto">
          <FilterSelect
            label=""
            value={filter.position}
            onChange={(v) => setFilter({ position: v })}
            options={POSITION_OPTIONS}
          />
        </div>
      </div>

      <div className="mb-3">
        <SearchInput
          placeholder="Search players..."
          onSearch={handleSearch}
          delay={200}
        />
      </div>

      {/* Column headers — auction (dollars) vs snake (ADP) */}
      <div className="flex items-center gap-2 px-3 py-1 text-[10px] uppercase tracking-wider text-slate-600 border-b border-[#2d3148]">
        <span className="w-8">Pos</span>
        <span className="flex-1">Player</span>
        <span className="w-10">Team</span>
        {isSnake ? (
          <>
            <span className="w-14 text-right">AI ADP</span>
            <span className="w-14 text-right">FP ADP</span>
            <span className="w-10 text-right">Diff</span>
          </>
        ) : (
          <>
            <span className="w-14 text-right">Ceiling</span>
            <span className="w-14 text-right">Market</span>
            <span className="w-10 text-right">Gap</span>
          </>
        )}
      </div>

      {/* Player list */}
      <div className="flex-1 overflow-y-auto">
        {filtered.map((p) => {
          const ceiling = getBidCeiling(p) ?? p.recommended_bid_ceiling ?? null
          const market = p.market_value ?? null
          const gap =
            ceiling != null && market != null ? ceiling - market : null

          // Snake metrics via playerUtils — AI ADP is the clean adp_rank, Diff
          // is the server-computed adp_diff (positive = we like them more).
          const adpDiff = getAdpDiff(p) // raw value drives the color thresholds

          return (
            <div
              key={p.id || p.yahoo_player_id}
              className="flex items-center gap-2 px-3 py-1.5 hover:bg-[#222539] cursor-pointer transition-colors border-b border-[#2d3148]/30"
            >
              <div className="w-8">
                <PositionBadge position={p.position} />
              </div>
              <span className="text-sm text-slate-300 flex-1 truncate">
                {p.name}
              </span>
              <span className="text-xs text-slate-600 w-10">{p.team_abbr}</span>
              {isSnake ? (
                <>
                  <span className="text-sm font-mono text-blue-400 w-14 text-right">
                    {formatAdp(p)}
                  </span>
                  <span className="text-xs font-mono text-slate-500 w-14 text-right">
                    {formatFpAdp(p)}
                  </span>
                  <span
                    className={`text-xs font-mono w-10 text-right ${
                      adpDiff != null && adpDiff > 3
                        ? 'text-emerald-400'
                        : adpDiff != null && adpDiff < -3
                        ? 'text-red-400'
                        : 'text-slate-600'
                    }`}
                  >
                    {formatAdpDiff(p)}
                  </span>
                </>
              ) : (
                <>
                  <span className="text-sm font-mono text-blue-400 w-14 text-right">
                    {ceiling != null ? `$${Math.round(ceiling)}` : '--'}
                  </span>
                  <span className="text-xs font-mono text-slate-500 w-14 text-right">
                    {market != null ? `$${Math.round(market)}` : '--'}
                  </span>
                  <span
                    className={`text-xs font-mono w-10 text-right ${
                      gap != null && gap > 3
                        ? 'text-emerald-400'
                        : gap != null && gap < -3
                        ? 'text-red-400'
                        : 'text-slate-600'
                    }`}
                  >
                    {gap != null
                      ? `${gap > 0 ? '+' : ''}${Math.round(gap)}`
                      : '--'}
                  </span>
                </>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
