/**
 * Trade page — build a trade or get ideas, over the proven endpoints
 * (GET /trade/league, POST /trade/analyze, POST /trade/ideas).
 *
 * Two tabs: [Build a trade] [Trade ideas]. The "Acting as" perspective switch is
 * DEMO-ONLY scaffolding (TRADE_DEMO_MODE / fetchTradeLeague) — slice-6 teardown
 * removes it; the opponent selector and the rest of the page are permanent.
 */
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeftRight, TrendingUp, TrendingDown, Minus, Lightbulb, Scale, Lock, X,
} from 'lucide-react'
import { fetchTradeLeague, analyzeTrade, fetchTradeIdeas } from '../api/trade'
import { useMe } from '../hooks/useMe'
import { CREDIT_COSTS } from '../lib/constants'
import VerdictPanel from '../components/trade/VerdictPanel'
import SilenceExplainer from '../components/trade/SilenceExplainer'

// Proactive locked affordance — the tier lacks this feature (and demo is off).
// Display only; the backend gate is the boundary.
function UpgradeInline({ label, tier }) {
  return (
    <Link
      to="/account"
      className="inline-flex items-center gap-2 rounded-md border border-brand-accent/40 bg-brand/10 px-4 py-2.5 text-sm font-medium text-brand-accent transition-colors hover:bg-brand/20"
    >
      <Lock size={14} /> {`${label} needs ${tier} — Upgrade`}
    </Link>
  )
}

const TREND = {
  rising: { Icon: TrendingUp, cls: 'text-emerald-400' },
  falling: { Icon: TrendingDown, cls: 'text-red-400' },
  stable: { Icon: Minus, cls: 'text-slate-500' },
}
const POS_CLS = {
  QB: 'bg-rose-500/15 text-rose-300', RB: 'bg-emerald-500/15 text-emerald-300',
  WR: 'bg-sky-500/15 text-sky-300', TE: 'bg-amber-500/15 text-amber-300',
}
const CONF_CLS = { full: 'text-slate-400', limited: 'text-amber-400', insufficient: 'text-slate-600' }

function PosBadge({ pos }) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${POS_CLS[pos] || 'bg-slate-500/15 text-slate-300'}`}>
      {pos}
    </span>
  )
}

function PlayerRow({ p, selected, onToggle, accent }) {
  const t = TREND[p.value_trend] || TREND.stable
  const ring = selected
    ? accent === 'get' ? 'border-emerald-500/70 bg-emerald-500/10' : 'border-brand-accent/70 bg-brand/15'
    : 'border-transparent bg-surface-2 hover:bg-surface-3'
  return (
    <button
      type="button"
      onClick={() => onToggle(p.id)}
      className={`flex w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors min-h-11 ${ring}`}
    >
      <PosBadge pos={p.position} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-sm font-medium text-white">{p.name}</span>
          {p.buy_low && <span className="rounded bg-emerald-500/15 px-1 text-[9px] font-semibold text-emerald-400">BUY</span>}
          {p.sell_high && <span className="rounded bg-amber-500/15 px-1 text-[9px] font-semibold text-amber-400">SELL</span>}
        </div>
        <div className="flex items-center gap-1.5 text-[10px] text-slate-500">
          <span>{p.nfl_team || '—'}</span>
          {p.starter_slot && p.starter_slot !== 'BENCH' && (
            <span className="rounded bg-surface-3 px-1 text-brand-accent">{p.starter_slot}</span>
          )}
          <span className={CONF_CLS[p.confidence]}>{p.confidence}</span>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <span className="tabular-nums text-sm font-semibold text-white">{Math.round(p.forward_value)}</span>
        <t.Icon size={14} className={t.cls} />
      </div>
    </button>
  )
}

function RosterColumn({ players, selected, onToggle, accent }) {
  // Starters first, then by forward_value desc — scannable.
  const sorted = useMemo(() => {
    const isStarter = (p) => p.starter_slot && p.starter_slot !== 'BENCH'
    return [...players].sort((a, b) =>
      (isStarter(b) - isStarter(a)) || (b.forward_value - a.forward_value))
  }, [players])
  return (
    <div className="max-h-[30rem] space-y-1 overflow-y-auto rounded-lg border border-border bg-surface-1 p-2">
      {sorted.map((p) => (
        <PlayerRow key={p.id} p={p} selected={selected.includes(p.id)} onToggle={onToggle} accent={accent} />
      ))}
    </div>
  )
}

function Chips({ ids, team, accent, onRemove }) {
  const byId = useMemo(() => Object.fromEntries((team?.roster || []).map((p) => [p.id, p])), [team])
  if (ids.length === 0) return <span className="text-xs text-slate-600">none selected</span>
  return (
    <div className="flex flex-wrap gap-1">
      {ids.map((id) => (
        <span key={id} className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${accent === 'get' ? 'bg-emerald-500/15 text-emerald-300' : 'bg-brand/20 text-brand-accent'}`}>
          {byId[id]?.name || id}
          <button type="button" onClick={() => onRemove(id)} className="hover:text-white"><X size={11} /></button>
        </span>
      ))}
    </div>
  )
}

export default function Trade() {
  const { data: league, isLoading, error } = useQuery({
    queryKey: ['trade-league'], queryFn: fetchTradeLeague, retry: false,
  })

  const [tab, setTab] = useState('build')
  const [myTeamId, setMyTeamId] = useState(null)
  const [opponentId, setOpponentId] = useState(null)
  const [give, setGive] = useState([])
  const [getIds, setGetIds] = useState([])

  const effMyId = myTeamId || league?.teams?.find((t) => t.is_me)?.team_id || league?.teams?.[0]?.team_id
  const myTeam = useMemo(() => league?.teams?.find((t) => t.team_id === effMyId), [league, effMyId])
  const otherTeams = useMemo(() => league?.teams?.filter((t) => t.team_id !== effMyId) || [], [league, effMyId])
  const effOppId = (opponentId && opponentId !== effMyId) ? opponentId : otherTeams[0]?.team_id
  const opponent = useMemo(() => otherTeams.find((t) => t.team_id === effOppId), [otherTeams, effOppId])

  const qc = useQueryClient()
  const { tierLimits } = useMe()
  // Refresh the shared credit balance (sidebar) after a spend.
  const refreshCredits = () => qc.invalidateQueries({ queryKey: ['me'] })

  const analyzeMut = useMutation({
    mutationFn: () => analyzeTrade({ myTeamId: effMyId, give, get: getIds }),
    onSuccess: refreshCredits,
  })
  const ideasMut = useMutation({
    mutationFn: () => fetchTradeIdeas({ myTeamId: effMyId }),
    onSuccess: refreshCredits,
  })

  // Switching perspective/opponent clears staged players (they belong to the
  // previous roster) — done in the handlers to avoid setState-in-effect.
  function switchActingAs(id) {
    setMyTeamId(id); setOpponentId(null); setGive([]); setGetIds([])
    analyzeMut.reset(); ideasMut.reset()
  }
  function switchOpponent(id) { setOpponentId(id); setGetIds([]); analyzeMut.reset() }

  const toggle = (setter) => (id) =>
    setter((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]))

  if (isLoading) return <div className="p-6 text-slate-400">Loading trade league…</div>
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

  // Demo bypasses the backend gate + credit charge — UNLESS enforcement is on
  // (TRADE_DEMO_ENFORCE_GATES), in which case demo behaves like the real thing.
  // Show a proactive locked CTA whenever the gate is live and the tier lacks it.
  const demo = !!league.demo_mode
  const enforced = !!league.enforced
  const gateLive = !demo || enforced
  const analyzeLocked = gateLive && tierLimits && tierLimits.trade_analyzer === false
  const ideasLocked = gateLive && tierLimits && tierLimits.trade_finder === false
  const costLabel = (n) => (demo && !enforced ? 'demo · no charge' : `${n} cr`)

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4 lg:p-6">
      {/* Header + demo perspective switch */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold text-white">
            <ArrowLeftRight size={22} className="text-brand-accent" /> Trade
          </h1>
          <p className="text-sm text-slate-500">
            Week {league.week}, {league.season} · value from in-season usage trajectory
          </p>
        </div>
        {league.demo_mode && (
          <label className="flex items-center gap-2 text-sm text-slate-400">
            Acting as
            <select
              value={effMyId}
              onChange={(e) => switchActingAs(e.target.value)}
              className="min-h-9 rounded-md border border-border bg-surface-2 px-2 py-1 text-white"
            >
              {league.teams.map((t) => (
                <option key={t.team_id} value={t.team_id}>{t.team_name}{t.is_me ? ' (you)' : ''}</option>
              ))}
            </select>
          </label>
        )}
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-border">
        {[['build', 'Build a trade', Scale], ['ideas', 'Trade ideas', Lightbulb]].map(([key, label, Icon]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={`-mb-px flex items-center gap-1.5 border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
              tab === key ? 'border-brand-accent text-white' : 'border-transparent text-slate-400 hover:text-slate-200'
            }`}
          >
            <Icon size={15} /> {label}
          </button>
        ))}
      </div>

      {tab === 'build' && (
        <div className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-2">
            {/* Your roster → give */}
            <section>
              <h2 className="mb-1.5 text-sm font-semibold text-brand-accent">
                Your roster — {myTeam?.team_name}
              </h2>
              <RosterColumn players={myTeam?.roster || []} selected={give} onToggle={toggle(setGive)} accent="give" />
            </section>

            {/* Opponent selector + roster → get */}
            <section>
              <div className="mb-1.5 flex items-center justify-between gap-2">
                <h2 className="shrink-0 text-sm font-semibold text-emerald-400">Trade with</h2>
                <select
                  value={effOppId}
                  onChange={(e) => switchOpponent(e.target.value)}
                  className="min-h-9 min-w-0 flex-1 rounded-md border border-border bg-surface-2 px-2 py-1 text-sm text-white"
                >
                  {otherTeams.map((t) => (
                    <option key={t.team_id} value={t.team_id}>{t.team_name}</option>
                  ))}
                </select>
              </div>
              <RosterColumn players={opponent?.roster || []} selected={getIds} onToggle={toggle(setGetIds)} accent="get" />
            </section>
          </div>

          {/* Trade summary + analyze */}
          <div className="rounded-lg border border-border bg-surface-1 p-3">
            <div className="grid items-center gap-3 sm:grid-cols-[1fr_auto_1fr_auto]">
              <div>
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">You give</div>
                <Chips ids={give} team={myTeam} accent="give" onRemove={toggle(setGive)} />
              </div>
              <ArrowLeftRight size={18} className="mx-auto hidden text-slate-600 sm:block" />
              <div>
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">You get</div>
                <Chips ids={getIds} team={opponent} accent="get" onRemove={toggle(setGetIds)} />
              </div>
              {analyzeLocked ? (
                <UpgradeInline label="Trade analyzer" tier="Standard" />
              ) : (
                <button
                  type="button"
                  disabled={give.length === 0 || getIds.length === 0 || analyzeMut.isPending}
                  onClick={() => analyzeMut.mutate()}
                  className="min-h-11 rounded-md bg-brand px-4 py-2 font-medium text-white transition-colors hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {analyzeMut.isPending
                    ? 'Analyzing…'
                    : `Analyze my trade · ${costLabel(CREDIT_COSTS.trade_analysis)}`}
                </button>
              )}
            </div>
          </div>

          {analyzeMut.isError && (
            <div className="rounded-md border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300">
              {analyzeMut.error?.response?.data?.detail || analyzeMut.error?.response?.data?.message || 'Analysis failed.'}
            </div>
          )}
          {analyzeMut.data && <VerdictPanel verdict={analyzeMut.data} />}
        </div>
      )}

      {tab === 'ideas' && (
        <div className="space-y-3">
          {ideasLocked ? (
            <UpgradeInline label="Trade finder" tier="Pro" />
          ) : (
            <button
              type="button"
              disabled={ideasMut.isPending}
              onClick={() => ideasMut.mutate()}
              className="min-h-11 rounded-md border border-brand-accent/40 bg-brand/10 px-4 py-2.5 font-medium text-brand-accent transition-colors hover:bg-brand/20 disabled:opacity-40"
            >
              {ideasMut.isPending
                ? 'Finding trades…'
                : `Give me trade ideas · ${costLabel(CREDIT_COSTS.trade_finder)}`}
            </button>
          )}

          {ideasMut.isError && (
            <div className="rounded-md border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300">
              {ideasMut.error?.response?.data?.detail || ideasMut.error?.response?.data?.message || 'Could not fetch ideas.'}
            </div>
          )}

          {/* Empty state is first-class — explain the silence, don't look broken */}
          {ideas && ideas.proposals.length === 0 && (
            <SilenceExplainer context={ideas.silence_context} fallbackMessage={ideas.message} />
          )}

          {ideas?.proposals.map((idea, i) => (
            <div key={i} className="space-y-1">
              <div className="text-xs text-slate-500">vs <span className="text-slate-300">{idea.counterparty_team_name}</span></div>
              <VerdictPanel verdict={idea.verdict} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
