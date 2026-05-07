import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Play, PlayCircle, RefreshCw, DollarSign, CheckCircle, AlertCircle, Clock } from 'lucide-react'
import { fetchPipelineStatus, triggerPipelineRun, fetchCostReport } from '../api/admin'

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
      <div className="bg-[#161822] rounded-lg border border-[#2d3148] overflow-hidden mb-6">
        <div className="px-4 py-3 border-b border-[#2d3148] flex items-center justify-between">
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

        <div className="grid grid-cols-[1fr_140px_80px_80px_120px] gap-2 px-4 py-2 text-[10px] uppercase tracking-wider text-slate-500 border-b border-[#2d3148]">
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
              className="grid grid-cols-[1fr_140px_80px_80px_120px] gap-2 px-4 py-2.5 items-center border-b border-[#2d3148]/50"
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
              <button
                onClick={() => runMutation.mutate({ agent: agent.agent_name })}
                disabled={runMutation.isPending}
                className="flex items-center gap-1 px-3 py-1 text-xs bg-blue-600/20 text-blue-400 rounded border border-blue-500/30 hover:bg-blue-600/30 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                <Play size={12} />
                Run
              </button>
            </div>
          ))
        )}
      </div>

      {/* Cost Report */}
      <div className="bg-[#161822] rounded-lg border border-[#2d3148] overflow-hidden">
        <div className="px-4 py-3 border-b border-[#2d3148] flex items-center justify-between">
          <div className="flex items-center gap-2">
            <DollarSign size={16} className="text-emerald-400" />
            <h3 className="text-sm font-medium text-slate-200">API Cost Report</h3>
          </div>
          <select
            value={costDays}
            onChange={(e) => setCostDays(parseInt(e.target.value))}
            className="bg-[#1c1f2e] text-xs text-slate-300 border border-[#2d3148] rounded px-2 py-1 focus:outline-none"
          >
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
        </div>

        <div className="grid grid-cols-[1fr_80px_80px_100px_100px_80px] gap-2 px-4 py-2 text-[10px] uppercase tracking-wider text-slate-500 border-b border-[#2d3148]">
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
                className="grid grid-cols-[1fr_80px_80px_100px_100px_80px] gap-2 px-4 py-2.5 items-center border-b border-[#2d3148]/50"
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
            <div className="grid grid-cols-[1fr_80px_80px_100px_100px_80px] gap-2 px-4 py-3 items-center bg-[#1c1f2e]">
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
