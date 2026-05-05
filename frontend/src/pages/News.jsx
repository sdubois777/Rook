import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchNews } from '../api/news'
import FilterBar, { FilterSelect } from '../components/shared/FilterBar'
import NewsFeedItem from '../components/shared/NewsFeedItem'
import Pagination from '../components/shared/Pagination'

const SIGNAL_TYPE_OPTIONS = [
  { value: '', label: 'All Types' },
  { value: 'injury_update', label: 'Injury Update' },
  { value: 'practice_status', label: 'Practice Status' },
  { value: 'depth_chart_move', label: 'Depth Chart' },
  { value: 'trade', label: 'Trade' },
  { value: 'release', label: 'Release' },
  { value: 'contract', label: 'Contract' },
  { value: 'coaching_change', label: 'Coaching Change' },
]

const DAYS_OPTIONS = [
  { value: '7', label: 'Last 7 days' },
  { value: '14', label: 'Last 14 days' },
  { value: '30', label: 'Last 30 days' },
  { value: '90', label: 'Last 90 days' },
]

export default function News() {
  const [signalType, setSignalType] = useState('')
  const [team, setTeam] = useState('')
  const [days, setDays] = useState('30')
  const [page, setPage] = useState(1)

  const { data, isLoading } = useQuery({
    queryKey: ['news', signalType, team, days, page],
    queryFn: () =>
      fetchNews({
        signal_type: signalType || undefined,
        team: team || undefined,
        days: parseInt(days),
        page,
        per_page: 50,
      }),
    refetchInterval: 60_000, // Auto-refresh every 60s
  })

  const signals = data?.signals || []
  const total = data?.total || 0
  const pages = data?.pages || 1

  return (
    <div className="max-w-4xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-semibold text-slate-100">News Feed</h1>
        <span className="text-sm text-slate-500">{total} signals</span>
      </div>

      <FilterBar>
        <FilterSelect
          label="Type"
          value={signalType}
          onChange={(v) => { setSignalType(v); setPage(1) }}
          options={SIGNAL_TYPE_OPTIONS}
        />
        <FilterSelect
          label="Period"
          value={days}
          onChange={(v) => { setDays(v); setPage(1) }}
          options={DAYS_OPTIONS}
        />
        <div className="flex items-center gap-2">
          <label className="text-xs text-slate-500">Team</label>
          <input
            type="text"
            value={team}
            onChange={(e) => { setTeam(e.target.value.toUpperCase()); setPage(1) }}
            placeholder="e.g. KC"
            maxLength={3}
            className="w-16 bg-[#1c1f2e] text-sm text-slate-300 border border-[#2d3148] rounded px-2 py-1 focus:outline-none focus:border-blue-500/50 placeholder-slate-600 uppercase"
          />
        </div>
      </FilterBar>

      <div className="bg-[#161822] rounded-lg border border-[#2d3148] overflow-hidden">
        {isLoading ? (
          <div className="py-12 text-center text-slate-500 text-sm">Loading signals...</div>
        ) : signals.length === 0 ? (
          <div className="py-12 text-center text-slate-500 text-sm">
            No signals found for the selected filters.
          </div>
        ) : (
          signals.map((signal) => (
            <NewsFeedItem key={signal.id} signal={signal} />
          ))
        )}
      </div>

      <Pagination page={page} pages={pages} onPageChange={setPage} />
    </div>
  )
}
