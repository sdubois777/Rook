import { useState, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchPlayers, searchPlayers } from '../api/players'
import { useUIStore } from '../stores/ui'
import FilterBar, { FilterSelect } from '../components/shared/FilterBar'
import SearchInput from '../components/shared/SearchInput'
import PlayerCardExpanded from '../components/shared/PlayerCardExpanded'
import Pagination from '../components/shared/Pagination'
import PlayerDetailPanel from '../components/PlayerDetailPanel'

const POSITION_OPTIONS = [
  { value: '', label: 'All Positions' },
  { value: 'QB', label: 'QB' },
  { value: 'RB', label: 'RB' },
  { value: 'WR', label: 'WR' },
  { value: 'TE', label: 'TE' },
]

const TIER_OPTIONS = [
  { value: '', label: 'All Tiers' },
  { value: '1', label: 'Tier 1' },
  { value: '2', label: 'Tier 2' },
  { value: '3', label: 'Tier 3' },
  { value: '4', label: 'Tier 4' },
  { value: '5', label: 'Tier 5' },
]

const VALUE_OPTIONS = [
  { value: '', label: 'All Values' },
  { value: 'undervalued', label: 'Undervalued' },
  { value: 'overvalued', label: 'Overvalued' },
  { value: 'aligned', label: 'Aligned' },
]

const FLAG_OPTIONS = [
  { value: '', label: 'All Flags' },
  { value: 'flagged', label: 'Has Flags' },
  { value: 'clean', label: 'Clean' },
]

const SORT_OPTIONS = [
  { value: 'bid_ceiling', label: 'Bid Ceiling' },
  { value: 'system_value', label: 'System Value' },
  { value: 'market_value', label: 'Market Value' },
  { value: 'value_gap', label: 'Value Gap' },
  { value: 'name', label: 'Name' },
  { value: 'tier', label: 'Tier' },
]

export default function Players() {
  const [position, setPosition] = useState('')
  const [tier, setTier] = useState('')
  const [valueGap, setValueGap] = useState('')
  const [flag, setFlag] = useState('')
  const [sort, setSort] = useState('bid_ceiling')
  const [page, setPage] = useState(1)
  const [searchQuery, setSearchQuery] = useState('')

  const openPlayerDetail = useUIStore((s) => s.openPlayerDetail)
  const selectedPlayerId = useUIStore((s) => s.selectedPlayerId)
  const detailPanelOpen = useUIStore((s) => s.detailPanelOpen)

  // Main player list query
  const { data, isLoading } = useQuery({
    queryKey: ['players', position, tier, valueGap, flag, sort, page],
    queryFn: () =>
      fetchPlayers({
        position: position || undefined,
        tier: tier || undefined,
        value_gap_dir: valueGap || undefined,
        flag: flag || undefined,
        sort,
        order: sort === 'name' ? 'asc' : 'desc',
        page,
        per_page: 50,
      }),
    enabled: !searchQuery,
  })

  // Search query
  const { data: searchResults, isLoading: isSearching } = useQuery({
    queryKey: ['players-search', searchQuery],
    queryFn: () => searchPlayers(searchQuery),
    enabled: searchQuery.length >= 2,
  })

  const handleSearch = useCallback((q) => {
    setSearchQuery(q)
    if (q) setPage(1)
  }, [])

  const players = searchQuery && searchResults ? searchResults : data?.players || []
  const total = searchQuery ? players.length : data?.total || 0
  const pages = searchQuery ? 1 : data?.pages || 1

  return (
    <div className="max-w-7xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-semibold text-slate-100">Players</h1>
        <span className="text-sm text-slate-500">{total} players</span>
      </div>

      {/* Search + Filters */}
      <div className="mb-4 w-64">
        <SearchInput placeholder="Search players..." onSearch={handleSearch} />
      </div>

      {!searchQuery && (
        <FilterBar>
          <FilterSelect
            label="Position"
            value={position}
            onChange={(v) => { setPosition(v); setPage(1) }}
            options={POSITION_OPTIONS}
          />
          <FilterSelect
            label="Tier"
            value={tier}
            onChange={(v) => { setTier(v); setPage(1) }}
            options={TIER_OPTIONS}
          />
          <FilterSelect
            label="Value"
            value={valueGap}
            onChange={(v) => { setValueGap(v); setPage(1) }}
            options={VALUE_OPTIONS}
          />
          <FilterSelect
            label="Flags"
            value={flag}
            onChange={(v) => { setFlag(v); setPage(1) }}
            options={FLAG_OPTIONS}
          />
          <FilterSelect
            label="Sort"
            value={sort}
            onChange={(v) => { setSort(v); setPage(1) }}
            options={SORT_OPTIONS}
          />
        </FilterBar>
      )}

      {/* Player list */}
      <div className="bg-[#161822] rounded-lg border border-[#2d3148] overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-2 border-b border-[#2d3148] text-[10px] uppercase tracking-wider text-slate-500">
          <span className="w-8">Pos</span>
          <span className="min-w-[160px]">Player</span>
          <span className="w-10">Tier</span>
          <span className="w-16 text-right">Ceiling</span>
          <span className="w-16 text-right">System</span>
          <span className="w-16 text-right">Market</span>
          <span className="w-24">Gap</span>
          <span className="ml-auto">Flags</span>
        </div>

        {(isLoading || isSearching) ? (
          <div className="py-12 text-center text-slate-500 text-sm">Loading...</div>
        ) : players.length === 0 ? (
          <div className="py-12 text-center text-slate-500 text-sm">
            {searchQuery ? 'No players match your search.' : 'No players found.'}
          </div>
        ) : (
          players.map((p) => (
            <PlayerCardExpanded
              key={p.id}
              player={p}
              onClick={openPlayerDetail}
            />
          ))
        )}
      </div>

      {!searchQuery && <Pagination page={page} pages={pages} onPageChange={setPage} />}

      {/* Detail panel */}
      {detailPanelOpen && selectedPlayerId && (
        <PlayerDetailPanel playerId={selectedPlayerId} />
      )}
    </div>
  )
}
