import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Play, PlayCircle, RefreshCw, DollarSign, CheckCircle, AlertCircle, Clock, FlaskConical, BarChart3 } from 'lucide-react'
import { fetchPipelineStatus, triggerPipelineRun, fetchCostReport, fetchDryRun, fetchBacktest } from '../api/admin'

const AGENT_LABELS = {
  team_systems: 'Team Systems',
  roster_changes: 'Roster Changes',
  player_profiles: 'Player Profiles',
  injury_risk: 'Injury Risk',
  schedule: 'Schedule',
  beat_reporter: 'Beat Reporter',
}

export default function PipelineAdmin() {
  const queryClient = useQueryClient()
  const [costDays, setCostDays] = useState(30)
  const [toast, setToast] = useState(null)
  const [dryRunResult, setDryRunResult] = useState(null)

  const showToast = (message, type = 'success') => {
    setToast({ message, type })
    setTimeout(() => setToast(null), 4000)
  }

  const { data: statusData, isLoading: statusLoading } = useQuery({
    queryKey: ['pipeline-status'],
    queryFn: fetchPipelineStatus,
    refetchInterval: 30_000,
  })

  const { data: costData, isLoading: costLoading } = useQuery({
    queryKey: ['cost-report', costDays],
    queryFn: () => fetchCostReport(costDays),
  })

  const runMutation = useMutation({
    mutationFn: ({ agent }) => triggerPipelineRun(agent),
    onSuccess: (data, { agent }) => {
      showToast(`${AGENT_LABELS[agent] || agent} started`)
      queryClient.invalidateQueries({ queryKey: ['pipeline-status'] })
    },
    onError: (err) => {
      showToast(`Failed: ${err.message}`, 'error')
    },
  })

  const dryRunMutation = useMutation({
    mutationFn: ({ agent }) => fetchDryRun(agent),
    onSuccess: (data) => setDryRunResult(data),
    onError: (err) => showToast(`Dry run failed: ${err.message}`, 'error'),
  })

  const runAllMutation = useMutation({
    mutationFn: async () => {
      const agents = ['team_systems', 'roster_changes', 'player_profiles', 'injury_risk', 'schedule', 'beat_reporter']
      for (const agent of agents) {
        await triggerPipelineRun(agent)
      }
    },
    onSuccess: () => {
      showToast('All agents started')
      queryClient.invalidateQueries({ queryKey: ['pipeline-status'] })
    },
    onError: (err) => {
      showToast(`Failed: ${err.message}`, 'error')
    },
  })

  return (
    <div className="max-w-5xl">
      <h1 className="text-2xl font-semibold text-slate-100 mb-6">Pipeline Admin</h1>

      {/* Agent Status Table */}
      <div className="bg-surface-1 rounded-lg border border-border overflow-hidden mb-6">
        <div className="px-4 py-3 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <RefreshCw size={16} className="text-blue-400" />
            <h3 className="text-sm font-medium text-slate-200">Agent Status</h3>
          </div>
          <button
            onClick={() => runAllMutation.mutate()}
            disabled={runAllMutation.isPending}
            className="flex items-center gap-1 px-3 py-1.5 text-xs bg-blue-600/20 text-blue-400 rounded border border-blue-500/30 hover:bg-blue-600/30 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <PlayCircle size={14} />
            {runAllMutation.isPending ? 'Running...' : 'Run All'}
          </button>
        </div>

        <div className="grid grid-cols-[1fr_140px_80px_80px_120px] gap-2 px-4 py-2 text-[10px] uppercase tracking-wider text-slate-500 border-b border-border">
          <span>Agent</span>
          <span>Last Run</span>
          <span>Entities</span>
          <span>Status</span>
          <span>Action</span>
        </div>

        {statusLoading ? (
          <div className="py-8 text-center text-slate-500 text-sm">Loading status...</div>
        ) : (
          (statusData?.agents || []).map((agent) => (
            <div
              key={agent.agent_name}
              className="grid grid-cols-[1fr_140px_80px_80px_120px] gap-2 px-4 py-2.5 items-center border-b border-border/50"
            >
              <span className="text-sm text-slate-200 font-medium">
                {AGENT_LABELS[agent.agent_name] || agent.agent_name}
              </span>
              <span className="text-xs text-slate-400">
                {agent.last_run
                  ? new Date(agent.last_run).toLocaleDateString('en-US', {
                      month: 'short',
                      day: 'numeric',
                      hour: '2-digit',
                      minute: '2-digit',
                    })
                  : 'Never'}
              </span>
              <span className="text-xs text-slate-400">{agent.entity_count}</span>
              <span>
                {agent.stale ? (
                  <span className="flex items-center gap-1 text-xs text-amber-400">
                    <AlertCircle size={12} />
                    Stale
                  </span>
                ) : agent.last_run ? (
                  <span className="flex items-center gap-1 text-xs text-emerald-400">
                    <CheckCircle size={12} />
                    Fresh
                  </span>
                ) : (
                  <span className="flex items-center gap-1 text-xs text-slate-500">
                    <Clock size={12} />
                    Pending
                  </span>
                )}
              </span>
              <div className="flex items-center gap-1.5">
                <button
                  onClick={() => dryRunMutation.mutate({ agent: agent.agent_name })}
                  disabled={dryRunMutation.isPending}
                  className="flex items-center gap-1 px-2 py-1 text-xs bg-amber-600/15 text-amber-400 rounded border border-amber-500/30 hover:bg-amber-600/25 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  title="Estimate cost"
                >
                  <FlaskConical size={11} />
                </button>
                <button
                  onClick={() => runMutation.mutate({ agent: agent.agent_name })}
                  disabled={runMutation.isPending}
                  className="flex items-center gap-1 px-3 py-1 text-xs bg-blue-600/20 text-blue-400 rounded border border-blue-500/30 hover:bg-blue-600/30 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  <Play size={12} />
                  Run
                </button>
              </div>
            </div>
          ))
        )}
      </div>

      {/* Dry Run Result */}
      {dryRunResult && (
        <div className="bg-surface-1 rounded-lg border border-amber-500/30 p-4 mb-6">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <FlaskConical size={16} className="text-amber-400" />
              <h3 className="text-sm font-medium text-amber-400">Cost Estimate</h3>
            </div>
            <button onClick={() => setDryRunResult(null)} className="text-xs text-slate-500 hover:text-slate-300">&times;</button>
          </div>
          {dryRunResult.estimates?.map((est) => (
            <div key={est.agent_name} className="text-sm text-slate-300 mb-1">
              <span className="font-medium">{AGENT_LABELS[est.agent_name] || est.agent_name}:</span>{' '}
              ~{est.estimated_entities} entities, {est.estimated_haiku_calls} Haiku + {est.estimated_sonnet_calls} Sonnet calls
              <span className="text-emerald-400 ml-2 font-mono">${est.estimated_cost_usd.toFixed(4)}</span>
            </div>
          ))}
          <div className="text-sm font-medium text-emerald-400 mt-2 font-mono">
            Total: ${dryRunResult.total_estimated_cost_usd?.toFixed(4)}
          </div>
          <div className="text-[10px] text-slate-500 mt-1">{dryRunResult.disclaimer}</div>
        </div>
      )}

      {/* Cost Report */}
      <div className="bg-surface-1 rounded-lg border border-border overflow-hidden">
        <div className="px-4 py-3 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <DollarSign size={16} className="text-emerald-400" />
            <h3 className="text-sm font-medium text-slate-200">API Cost Report</h3>
          </div>
          <select
            value={costDays}
            onChange={(e) => setCostDays(parseInt(e.target.value))}
            className="bg-surface-2 text-xs text-slate-300 border border-border rounded px-2 py-1 focus:outline-none"
          >
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
        </div>

        <div className="grid grid-cols-[1fr_80px_80px_100px_100px_80px] gap-2 px-4 py-2 text-[10px] uppercase tracking-wider text-slate-500 border-b border-border">
          <span>Agent</span>
          <span>Calls</span>
          <span>Cache Hits</span>
          <span>Input Tokens</span>
          <span>Output Tokens</span>
          <span className="text-right">Cost</span>
        </div>

        {costLoading ? (
          <div className="py-8 text-center text-slate-500 text-sm">Loading costs...</div>
        ) : (costData?.agents || []).length === 0 ? (
          <div className="py-8 text-center text-slate-500 text-sm">No API usage in this period.</div>
        ) : (
          <>
            {(costData?.agents || []).map((agent) => (
              <div
                key={agent.agent_name}
                className="grid grid-cols-[1fr_80px_80px_100px_100px_80px] gap-2 px-4 py-2.5 items-center border-b border-border/50"
              >
                <span className="text-sm text-slate-300">
                  {AGENT_LABELS[agent.agent_name] || agent.agent_name}
                </span>
                <span className="text-xs text-slate-400">{agent.total_calls.toLocaleString()}</span>
                <span className="text-xs text-slate-400">{agent.cache_hits.toLocaleString()}</span>
                <span className="text-xs text-slate-400">{agent.total_input_tokens.toLocaleString()}</span>
                <span className="text-xs text-slate-400">{agent.total_output_tokens.toLocaleString()}</span>
                <span className="text-xs text-emerald-400 text-right font-mono">
                  ${agent.total_cost_usd.toFixed(4)}
                </span>
              </div>
            ))}

            {/* Grand total */}
            <div className="grid grid-cols-[1fr_80px_80px_100px_100px_80px] gap-2 px-4 py-3 items-center bg-surface-2">
              <span className="text-sm text-slate-200 font-medium">Total</span>
              <span />
              <span />
              <span />
              <span />
              <span className="text-sm text-emerald-400 text-right font-mono font-medium">
                ${costData?.grand_total_usd?.toFixed(4) || '0.0000'}
              </span>
            </div>
          </>
        )}
      </div>

      {/* System Validation / Backtest */}
      <BacktestSection />

      {/* Toast notification */}
      {toast && (
        <div className={`fixed bottom-6 right-6 z-50 px-4 py-2.5 rounded-lg shadow-lg text-sm font-medium animate-fade-in ${
          toast.type === 'error'
            ? 'bg-red-600/90 text-white'
            : 'bg-emerald-600/90 text-white'
        }`}>
          {toast.message}
        </div>
      )}
    </div>
  )
}


function BacktestSection() {
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['backtest'],
    queryFn: () => fetchBacktest(2025),
    enabled: false,
    staleTime: Infinity,
  })

  const gradeColor = {
    STRONG: 'text-emerald-400',
    MODERATE: 'text-amber-400',
    WEAK: 'text-red-400',
    POOR: 'text-red-500',
  }

  return (
    <div className="bg-surface-2 rounded-xl p-5 border border-border/50">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <BarChart3 size={18} className="text-purple-400" />
          <h2 className="text-lg font-semibold text-slate-200">System Validation</h2>
        </div>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-purple-500/20 hover:bg-purple-500/30 text-purple-400 rounded-lg text-sm transition-colors disabled:opacity-50"
        >
          {isFetching ? <RefreshCw size={14} className="animate-spin" /> : <FlaskConical size={14} />}
          {isFetching ? 'Running...' : 'Run Backtest'}
        </button>
      </div>

      {error && (
        <div className="text-red-400 text-sm mb-3">
          Error: {error.message}
        </div>
      )}

      {!data && !isLoading && !error && (
        <p className="text-sm text-slate-500">
          Run a backtest to compare system projections against actual 2025 season results.
        </p>
      )}

      {data && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-xs text-slate-500">2025 Season</span>
            <span className={`text-sm font-semibold ${gradeColor[data.grade] || 'text-slate-400'}`}>
              {data.grade}
            </span>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Stat label="Signal accuracy" value={data.signals?.accuracy != null ? `${data.signals.accuracy}%` : '--'} />
            <Stat label="Projection MAE" value={data.projection?.mae != null ? `${data.projection.mae} PPR` : '--'} />
            <Stat label="Buy signals right" value={data.signals?.buy_accuracy != null ? `${data.signals.buy_accuracy}%` : '--'} sub={`${data.signals?.buy_count || 0} calls`} />
            <Stat label="Avoid signals right" value={data.signals?.avoid_accuracy != null ? `${data.signals.avoid_accuracy}%` : '--'} sub={`${data.signals?.avoid_count || 0} calls`} />
            <Stat label="Top opportunities" value={`${data.top_opportunities?.delivered || 0}/${data.top_opportunities?.flagged || 0}`} sub="delivered value" />
            <Stat label="Correlation (r)" value={data.projection?.correlation != null ? data.projection.correlation.toFixed(3) : '--'} />
          </div>

          <div className="text-[10px] text-slate-600 mt-2">
            {data.players_matched}/{data.players_analyzed} players matched | {data.injury_excluded} injury-excluded
          </div>
        </div>
      )}
    </div>
  )
}


function Stat({ label, value, sub }) {
  return (
    <div className="bg-surface-1 rounded-lg p-2.5">
      <div className="text-[10px] text-slate-500 mb-0.5">{label}</div>
      <div className="text-sm font-mono font-semibold text-slate-200">{value}</div>
      {sub && <div className="text-[10px] text-slate-600">{sub}</div>}
    </div>
  )
}
