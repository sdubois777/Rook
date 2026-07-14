import { useState, useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { fetchNews, fetchNewsTypes } from '../api/news'
import { useUIStore } from '../stores/ui'
import FilterBar, { FilterSelect } from '../components/shared/FilterBar'
import NewsFeedItem from '../components/shared/NewsFeedItem'
import Pagination from '../components/shared/Pagination'
import PlayerDetailPanel from '../components/PlayerDetailPanel'

const DAYS_OPTIONS = [
  { value: '7', label: 'Last 7 days' },
  { value: '14', label: 'Last 14 days' },
  { value: '30', label: 'Last 30 days' },
  { value: '90', label: 'Last 90 days' },
]

const DEFAULT_DAYS = '30'

export default function News() {
  const [signalType, setSignalType] = useState('')
  const [team, setTeam] = useState('')
  const [days, setDays] = useState(DEFAULT_DAYS)
  const [page, setPage] = useState(1)

  // Type options are DERIVED from the data (real signal_type values the agent
  // writes) — the previous hardcoded list matched none of them, so every Type
  // selection returned nothing.
  const { data: typeFacets } = useQuery({
    queryKey: ['news-types'],
    queryFn: fetchNewsTypes,
    staleTime: 5 * 60 * 1000,
  })
  const typeOptions = [
    { value: '', label: 'All Types' },
    ...(typeFacets || []).map((t) => ({ value: t.value, label: t.label })),
  ]

  const filtersActive = signalType !== '' || team !== '' || days !== DEFAULT_DAYS
  const clearFilters = () => {
    setSignalType('')
    setTeam('')
    setDays(DEFAULT_DAYS)
    setPage(1)
  }
  const [wsConnected, setWsConnected] = useState(false)
  const openPlayerDetail = useUIStore((s) => s.openPlayerDetail)
  const selectedPlayerId = useUIStore((s) => s.selectedPlayerId)
  const detailPanelOpen = useUIStore((s) => s.detailPanelOpen)
  const queryClient = useQueryClient()
  const wsRef = useRef(null)

  // WebSocket for live news updates
  useEffect(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws/news`)
    wsRef.current = ws

    ws.onopen = () => setWsConnected(true)
    ws.onclose = () => setWsConnected(false)
    ws.onmessage = (event) => {
      try {
        const signal = JSON.parse(event.data)
        // Prepend new signal to cached query data
        queryClient.setQueryData(
          ['news', signalType, team, days, page],
          (old) => {
            if (!old) return old
            return {
              ...old,
              signals: [signal, ...old.signals],
              total: old.total + 1,
            }
          }
        )
      } catch { /* ignore parse errors */ }
    }

    return () => ws.close()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

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
    <div className="mx-auto max-w-4xl">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-semibold text-slate-100">News Feed</h1>
          <span className={`w-2 h-2 rounded-full ${wsConnected ? 'bg-emerald-400' : 'bg-slate-600'}`} title={wsConnected ? 'Live updates active' : 'Polling every 60s'} />
        </div>
        <span className="text-sm text-slate-500">
          {total} {filtersActive ? 'matching' : 'signals'}
        </span>
      </div>

      <FilterBar>
        <FilterSelect
          label="Type"
          value={signalType}
          onChange={(v) => { setSignalType(v); setPage(1) }}
          options={typeOptions}
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
            className="w-16 bg-surface-2 text-sm text-slate-300 border border-border rounded px-2 py-1 focus:outline-none focus:border-brand-accent/60 placeholder-slate-600 uppercase"
          />
        </div>
        {filtersActive && (
          <button
            type="button"
            onClick={clearFilters}
            className="ml-auto inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-xs text-slate-400 hover:border-slate-500 hover:text-slate-200 focus:outline-none focus:border-brand-accent/60"
          >
            <X size={12} /> Clear
          </button>
        )}
      </FilterBar>

      <div className="bg-surface-1 rounded-lg border border-border overflow-hidden">
        {isLoading ? (
          <div className="py-12 text-center text-slate-500 text-sm">Loading signals...</div>
        ) : signals.length === 0 ? (
          <div className="py-12 text-center text-slate-500 text-sm">
            {filtersActive ? (
              <>
                No signals match these filters.{' '}
                <button
                  type="button"
                  onClick={clearFilters}
                  className="text-brand-accent hover:underline"
                >
                  Clear filters
                </button>
              </>
            ) : (
              'No signals yet.'
            )}
          </div>
        ) : (
          signals.map((signal) => (
            <NewsFeedItem key={signal.id} signal={signal} onPlayerClick={openPlayerDetail} />
          ))
        )}
      </div>

      <Pagination page={page} pages={pages} onPageChange={setPage} />

      {detailPanelOpen && selectedPlayerId && (
        <PlayerDetailPanel playerId={selectedPlayerId} />
      )}
    </div>
  )
}
