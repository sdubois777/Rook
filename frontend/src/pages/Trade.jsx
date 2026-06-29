/**
 * Trade page — build a trade and get a verdict, or ask the system for ideas.
 * Renders over the proven endpoints: GET /trade/league (picker), POST
 * /trade/analyze, POST /trade/ideas.
 *
 * The team-switcher is DEMO-ONLY scaffolding (TRADE_DEMO_MODE / fetchTradeLeague)
 * — slice-6 teardown removes it; the rest of the page is permanent.
 */
import { useMemo, useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { ArrowLeftRight, TrendingUp, TrendingDown, Minus, Lightbulb, Scale } from 'lucide-react'
import { fetchTradeLeague, analyzeTrade, fetchTradeIdeas } from '../api/trade'
import VerdictPanel from '../components/trade/VerdictPanel'

const TREND = {
  rising: { Icon: TrendingUp, cls: 'text-emerald-400' },
  falling: { Icon: TrendingDown, cls: 'text-red-400' },
  stable: { Icon: Minus, cls: 'text-slate-500' },
}

function PlayerPickRow({ p, selected, onToggle, accent }) {
  const t = TREND[p.value_trend] || TREND.stable
  const ring = selected
    ? accent === 'get'
      ? 'border-emerald-500/60 bg-emerald-500/10'
      : 'border-brand-accent/60 bg-brand/10'
    : 'border-border bg-surface-2 hover:bg-surface-3'
  return (
    <button
      type="button"
      onClick={() => onToggle(p.id)}
      className={`flex w-full items-center justify-between gap-2 rounded-md border px-3 py-2 text-left transition-colors min-h-11 ${ring}`}
    >
      <div className="min-w-0">
        <div className="flex items-center gap-1.5">
          <span className="truncate font-medium text-white">{p.name}</span>
          <span className="text-xs text-slate-400">{p.position}</span>
          {p.buy_low && <span className="rounded bg-emerald-500/15 text-emerald-400 text-[10px] px-1 py-0.5">BUY</span>}
          {p.sell_high && <span className="rounded bg-amber-500/15 text-amber-400 text-[10px] px-1 py-0.5">SELL</span>}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2 text-sm">
        <span className="tabular-nums font-semibold text-white">{Math.round(p.forward_value)}</span>
        <t.Icon size={14} className={t.cls} />
      </div>
    </button>
  )
}

export default function Trade() {
  const { data: league, isLoading, error } = useQuery({
    queryKey: ['trade-league'],
    queryFn: fetchTradeLeague,
    retry: false,
  })

  const [myTeamId, setMyTeamId] = useState(null)
  const [give, setGive] = useState([])
  const [getIds, setGetIds] = useState([])

  // Default "me" to the seeded is_me team once the league loads.
  const effectiveMyTeamId = myTeamId || league?.teams?.find((t) => t.is_me)?.team_id || league?.teams?.[0]?.team_id

  const myTeam = useMemo(
    () => league?.teams?.find((t) => t.team_id === effectiveMyTeamId),
    [league, effectiveMyTeamId],
  )
  const otherTeams = useMemo(
    () => league?.teams?.filter((t) => t.team_id !== effectiveMyTeamId) || [],
    [league, effectiveMyTeamId],
  )

  const analyzeMut = useMutation({
    mutationFn: () => analyzeTrade({ myTeamId: effectiveMyTeamId, give, get: getIds }),
  })
  const ideasMut = useMutation({
    mutationFn: () => fetchTradeIdeas({ myTeamId: effectiveMyTeamId }),
  })

  function switchTeam(id) {
    setMyTeamId(id)
    setGive([])
    setGetIds([])
    analyzeMut.reset()
    ideasMut.reset()
  }
  const toggle = (setter) => (id) =>
    setter((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]))

  if (isLoading) {
    return <div className="p-6 text-slate-400">Loading trade league…</div>
  }
  if (error) {
    const demoOff = error?.response?.status === 404
    return (
      <div className="mx-auto max-w-2xl p-6">
        <div className="rounded-lg border border-border bg-surface-1 p-6 text-slate-300">
          <h1 className="mb-2 flex items-center gap-2 text-lg font-semibold text-white">
            <ArrowLeftRight size={20} /> Trade
          </h1>
          {demoOff
            ? 'The trade demo league is only available with TRADE_DEMO_MODE enabled.'
            : 'Could not load the trade league.'}
        </div>
      </div>
    )
  }

  const ideas = ideasMut.data

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-4 lg:p-6">
      {/* Header + demo team-switcher */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold text-white">
            <ArrowLeftRight size={22} className="text-brand-accent" /> Trade
          </h1>
          <p className="text-sm text-slate-500">
            Week {league.week}, {league.season} · in-season value from usage trajectory
          </p>
        </div>
        {league.demo_mode && (
          /* DEMO-ONLY team-switcher (teardown: slice 6) */
          <label className="flex items-center gap-2 text-sm text-slate-400">
            Acting as
            <select
              value={effectiveMyTeamId}
              onChange={(e) => switchTeam(e.target.value)}
              className="rounded-md border border-border bg-surface-2 px-2 py-1.5 text-white"
            >
              {league.teams.map((t) => (
                <option key={t.team_id} value={t.team_id}>
                  {t.team_name}{t.is_me ? ' (you)' : ''}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* ── Build a trade → Analyze ── */}
        <section className="space-y-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-slate-400">
            <Scale size={16} /> Build a trade
          </h2>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="mb-1.5 text-xs font-semibold text-brand-accent">You give ({myTeam?.team_name})</div>
              <div className="space-y-1.5">
                {myTeam?.roster.map((p) => (
                  <PlayerPickRow key={p.id} p={p} selected={give.includes(p.id)} onToggle={toggle(setGive)} accent="give" />
                ))}
              </div>
            </div>
            <div>
              <div className="mb-1.5 text-xs font-semibold text-emerald-400">You get</div>
              <div className="space-y-3">
                {otherTeams.map((t) => (
                  <div key={t.team_id}>
                    <div className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">{t.team_name}</div>
                    <div className="space-y-1.5">
                      {t.roster.map((p) => (
                        <PlayerPickRow key={p.id} p={p} selected={getIds.includes(p.id)} onToggle={toggle(setGetIds)} accent="get" />
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <button
            type="button"
            disabled={give.length === 0 || getIds.length === 0 || analyzeMut.isPending}
            onClick={() => analyzeMut.mutate()}
            className="w-full rounded-md bg-brand px-4 py-2.5 font-medium text-white transition-colors hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-40"
          >
            {analyzeMut.isPending ? 'Analyzing…' : 'Analyze my trade'}
          </button>

          {analyzeMut.isError && (
            <div className="rounded-md border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300">
              {analyzeMut.error?.response?.data?.detail || analyzeMut.error?.response?.data?.message || 'Analysis failed.'}
            </div>
          )}
          {analyzeMut.data && <VerdictPanel verdict={analyzeMut.data} />}
        </section>

        {/* ── Trade ideas → Proposals ── */}
        <section className="space-y-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-slate-400">
            <Lightbulb size={16} /> Trade ideas
          </h2>
          <button
            type="button"
            disabled={ideasMut.isPending}
            onClick={() => ideasMut.mutate()}
            className="w-full rounded-md border border-brand-accent/40 bg-brand/10 px-4 py-2.5 font-medium text-brand-accent transition-colors hover:bg-brand/20 disabled:opacity-40"
          >
            {ideasMut.isPending ? 'Finding trades…' : 'Give me trade ideas'}
          </button>

          {ideasMut.isError && (
            <div className="rounded-md border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300">
              {ideasMut.error?.response?.data?.detail || ideasMut.error?.response?.data?.message || 'Could not fetch ideas.'}
            </div>
          )}

          {/* Empty state is FIRST-CLASS — not an error, not a spinner */}
          {ideas && ideas.proposals.length === 0 && (
            <div className="rounded-lg border border-border bg-surface-1 px-4 py-6 text-center text-slate-400">
              <Minus size={20} className="mx-auto mb-1 text-slate-600" />
              {ideas.message || 'No clear trade right now.'}
              <div className="mt-1 text-xs text-slate-600">Nothing on the board clears the bar — that's a real answer, not a miss.</div>
            </div>
          )}

          {ideas?.proposals.map((idea, i) => (
            <div key={i} className="space-y-1">
              <div className="text-xs text-slate-500">
                vs <span className="text-slate-300">{idea.counterparty_team_name}</span>
              </div>
              <VerdictPanel verdict={idea.verdict} />
            </div>
          ))}
        </section>
      </div>
    </div>
  )
}
