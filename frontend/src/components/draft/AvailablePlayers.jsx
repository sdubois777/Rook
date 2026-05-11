import { useMemo, useCallback } from 'react'
import { useDraftStore } from '../../stores/draft'
import PositionBadge from '../shared/PositionBadge'
import SearchInput from '../shared/SearchInput'
import { FilterSelect } from '../shared/FilterBar'

const POSITION_OPTIONS = [
  { value: '', label: 'All' },
  { value: 'QB', label: 'QB' },
  { value: 'RB', label: 'RB' },
  { value: 'WR', label: 'WR' },
  { value: 'TE', label: 'TE' },
]

export default function AvailablePlayers() {
  const availablePlayers = useDraftStore((s) => s.availablePlayers)
  const filter = useDraftStore((s) => s.availableFilter)
  const setFilter = useDraftStore((s) => s.setAvailableFilter)

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

    // Sort by AI ceiling descending
    return [...list].sort((a, b) => {
      const ac = a.ai_bid_ceiling ?? a.recommended_bid_ceiling ?? 0
      const bc = b.ai_bid_ceiling ?? b.recommended_bid_ceiling ?? 0
      return bc - ac
    })
  }, [availablePlayers, filter])

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

      {/* Column headers */}
      <div className="flex items-center gap-2 px-3 py-1 text-[10px] uppercase tracking-wider text-slate-600 border-b border-[#2d3148]">
        <span className="w-8">Pos</span>
        <span className="flex-1">Player</span>
        <span className="w-10">Team</span>
        <span className="w-14 text-right">Ceiling</span>
        <span className="w-14 text-right">Market</span>
        <span className="w-10 text-right">Gap</span>
      </div>

      {/* Player list */}
      <div className="flex-1 overflow-y-auto">
        {filtered.map((p) => {
          const ceiling = p.ai_bid_ceiling ?? p.recommended_bid_ceiling ?? null
          const market = p.market_value ?? null
          const gap =
            ceiling != null && market != null ? ceiling - market : null

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
            </div>
          )
        })}
      </div>
    </div>
  )
}
