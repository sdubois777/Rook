/**
 * Waiver Wire page — rank the available free-agent pool by the real-ppw
 * improvement each add makes to your starting lineup (a waiver add/drop is a
 * one-sided trade), with a suggested FAAB bid and a news/opportunity tie-in.
 *
 * Mirrors the Trade page: GET /waiver/league (demo picker) + POST
 * /waiver/recommendations. The "Acting as" switch is DEMO-ONLY scaffolding
 * (WAIVER_DEMO_MODE / fetchWaiverLeague) — teardown removes it with the trade demo.
 */
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Waves, TrendingUp, TrendingDown, Minus, Lock, ArrowRight, Newspaper, Target, Shield,
} from 'lucide-react'
import { fetchWaiverLeague, fetchWaiverRecommendations } from '../api/waiver'
import { useMe } from '../hooks/useMe'
import { CREDIT_COSTS } from '../lib/constants'
import { PlayerBadges } from '../components/shared/PlayerName'

const TREND = {
  rising: { Icon: TrendingUp, cls: 'text-emerald-400' },
  falling: { Icon: TrendingDown, cls: 'text-red-400' },
  stable: { Icon: Minus, cls: 'text-slate-500' },
}
const CONF_CLS = { high: 'text-emerald-400', medium: 'text-amber-400', low: 'text-slate-500',
  full: 'text-slate-400', limited: 'text-amber-400', insufficient: 'text-slate-600' }

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

function NewsBadge({ news }) {
  const conf = news.confidence ? <span className={CONF_CLS[news.confidence]}>{news.confidence}</span> : null
  return (
    <div className="mt-2 rounded-md border border-sky-500/25 bg-sky-500/5 px-2.5 py-2 text-xs">
      <div className="mb-0.5 flex items-center gap-1.5 font-semibold text-sky-300">
        {news.kind === 'opportunity' ? <Target size={12} /> : <Newspaper size={12} />}
        {news.kind === 'opportunity'
          ? `Opportunity${news.starter_name ? ` — next up if ${news.starter_name} sits` : ''}`
          : 'Fresh signal'}
        <span className="rounded bg-surface-3 px-1 text-[10px] uppercase tracking-wide text-slate-400">
          {news.signal_type?.replace(/_/g, ' ')}
        </span>
        {conf}
      </div>
      {/* raw_text is the article TITLE only (no body is stored) — no expand control. */}
      <div className="text-slate-300">{news.headline}</div>
      {news.contingent_impact_pct != null && (
        <div className="mt-1 text-emerald-300">
          +{news.contingent_impact_pct}% projected value{news.contingent_reasoning ? ` — ${news.contingent_reasoning}` : ''}
        </div>
      )}
    </div>
  )
}

// DST matchup context (slice 4 tilt, display-only). HONEST framing: the tilt is a
// gentle ~±2.5 ppw dart on a ~6-7 pt base (a ~0.20-correlation signal) — restrained
// wording only. Favorable/tough at |tilt| >= 0.5; near-zero reads neutral, never hyped.
function MatchupTag({ matchup }) {
  const tilt = matchup.tilt_ppw
  const vs = matchup.opponent ? ` vs ${matchup.opponent}` : ''
  let label, cls
  if (tilt >= 0.5) { label = `Favorable matchup${vs}`; cls = 'border-emerald-500/25 bg-emerald-500/5 text-emerald-300' }
  else if (tilt <= -0.5) { label = `Tough matchup${vs}`; cls = 'border-amber-500/25 bg-amber-500/5 text-amber-300' }
  else { label = `Even matchup${vs}`; cls = 'border-border bg-surface-2 text-slate-400' }
  return (
    <div className={`mt-2 flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs ${cls}`}>
      <Shield size={12} />
      <span className="font-medium">{label}</span>
      <span className="tabular-nums text-[11px] opacity-80">{tilt > 0 ? '+' : ''}{tilt} ppw</span>
    </div>
  )
}

function RecCard({ rec }) {
  const t = TREND[rec.add.value_trend] || TREND.stable
  const f = rec.faab
  return (
    <div className="rounded-lg border border-border bg-surface-1 p-3">
      <div className="flex items-start justify-between gap-3">
        {/* Add player */}
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <PlayerBadges position={rec.add.position} injuryStatus={rec.add.injury_status} variant="compact" />
            <span className="truncate text-sm font-semibold text-white">{rec.add.name}</span>
            <t.Icon size={14} className={t.cls} />
            {rec.add.buy_low && <span className="rounded bg-emerald-500/15 px-1 text-[9px] font-semibold text-emerald-400">BUY</span>}
            {rec.fills_need && <span className="rounded bg-brand/20 px-1.5 text-[9px] font-semibold text-brand-accent">FILLS NEED</span>}
          </div>
          <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-slate-500">
            <span>{rec.add.nfl_team || '—'}</span>
            <span className={CONF_CLS[rec.add.confidence]}>{rec.add.confidence}</span>
            <span className="tabular-nums">{rec.add.forward_ppg} ppg</span>
          </div>
        </div>
        {/* FAAB bid */}
        <div className="shrink-0 text-right">
          {f.recommended ? (
            <>
              <div className="text-lg font-bold text-emerald-400">${f.total_bid}</div>
              <div className="text-[10px] text-slate-500">
                {Math.round(f.pct_of_remaining * 100)}% · {f.tier_label}
              </div>
              {f.news_bump_bid > 0 && (
                <div className="text-[10px] text-sky-400">incl. +${f.news_bump_bid} news</div>
              )}
            </>
          ) : (
            <div className="text-xs text-slate-600">no bid</div>
          )}
        </div>
      </div>

      {/* Delta + drop */}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className={`font-semibold tabular-nums ${rec.lineup_delta_ppw > 0 ? 'text-emerald-400' : 'text-slate-500'}`}>
          {rec.lineup_delta_ppw > 0 ? '+' : ''}{rec.lineup_delta_ppw} ppw to your lineup
        </span>
        <span className="flex items-center gap-1 text-slate-400">
          <ArrowRight size={12} className="text-slate-600" />
          {rec.drop
            ? <>drop <span className="text-slate-300">{rec.drop.name}</span> <PlayerBadges position={rec.drop.position} injuryStatus={rec.drop.injury_status} variant="compact" /></>
            : <span className="text-slate-500">open roster slot — no drop needed</span>}
        </span>
      </div>

      {/* DST-only matchup context (backend sets `matchup` only for tilted DSTs; K + offense have none). */}
      {rec.matchup && <MatchupTag matchup={rec.matchup} />}
      {rec.news && <NewsBadge news={rec.news} />}
    </div>
  )
}

export default function Waiver() {
  const { data: league, isLoading, error } = useQuery({
    queryKey: ['waiver-league'], queryFn: fetchWaiverLeague, retry: false,
  })

  const [myTeamId, setMyTeamId] = useState(null)
  const effMyId = myTeamId || league?.teams?.find((t) => t.is_me)?.team_id || league?.teams?.[0]?.team_id
  const myTeam = useMemo(() => league?.teams?.find((t) => t.team_id === effMyId), [league, effMyId])

  const qc = useQueryClient()
  const { tierLimits } = useMe()
  const recMut = useMutation({
    mutationFn: () => fetchWaiverRecommendations({ myTeamId: effMyId }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['me'] }),
  })

  function switchActingAs(id) { setMyTeamId(id); recMut.reset() }

  if (isLoading) return <div className="p-6 text-slate-400">Loading waiver wire…</div>
  if (error) {
    const demoOff = error?.response?.status === 404
    return (
      <div className="mx-auto max-w-2xl p-6">
        <div className="rounded-lg border border-border bg-surface-1 p-6 text-slate-300">
          <h1 className="mb-2 flex items-center gap-2 text-lg font-semibold text-white">
            <Waves size={20} /> Waiver Wire
          </h1>
          {demoOff
            ? 'The waiver demo league is only available with WAIVER_DEMO_MODE enabled.'
            : 'Could not load the waiver wire.'}
        </div>
      </div>
    )
  }

  const demo = !!league.demo_mode
  const enforced = !!league.enforced
  const gateLive = !demo || enforced
  const locked = gateLive && tierLimits && tierLimits.waiver_wire === false
  const costLabel = demo && !enforced ? 'demo · no charge' : `${CREDIT_COSTS.waiver_wire} cr`
  const remaining = myTeam?.faab_remaining ?? league.faab_budget
  const data = recMut.data

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4 lg:p-6">
      {/* Header + demo perspective switch */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold text-white">
            <Waves size={22} className="text-brand-accent" /> Waiver Wire
          </h1>
          <p className="text-sm text-slate-500">
            Week {league.week}, {league.season} · {league.waiver_type?.toUpperCase()} · ${remaining} of ${league.faab_budget} budget left
          </p>
        </div>
        {demo && (
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

      {/* Run */}
      <div className="flex flex-wrap items-center gap-3">
        {locked ? (
          <UpgradeInline label="Waiver wire" tier="Standard" />
        ) : (
          <button
            type="button"
            disabled={recMut.isPending}
            onClick={() => recMut.mutate()}
            className="min-h-11 rounded-md bg-brand px-4 py-2 font-medium text-white transition-colors hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-40"
          >
            {recMut.isPending ? 'Scanning waivers…' : `Find waiver targets · ${costLabel}`}
          </button>
        )}
        {data?.needs?.length > 0 && (
          <span className="text-xs text-slate-500">
            Roster needs: {data.needs.map((n) => <span key={n} className="mr-1 rounded bg-surface-2 px-1.5 py-0.5 text-brand-accent">{n}</span>)}
          </span>
        )}
      </div>

      {recMut.isError && (
        <div className="rounded-md border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300">
          {recMut.error?.response?.data?.detail || recMut.error?.response?.data?.message || 'Could not fetch waiver targets.'}
        </div>
      )}

      {/* Empty state — explain the silence, don't look broken */}
      {data && data.recommendations.length === 0 && (
        <div className="rounded-lg border border-border bg-surface-1 p-6 text-sm text-slate-300">
          <div className="mb-1 font-semibold text-white">{data.silence?.reason || 'Nothing worth claiming right now.'}</div>
          {data.silence?.near_miss_name && (
            <div className="text-slate-500">
              Closest: <span className="text-slate-300">{data.silence.near_miss_name}</span>
              {' '}({data.silence.near_miss_gain > 0 ? '+' : ''}{data.silence.near_miss_gain} ppw) — not enough to spend on.
            </div>
          )}
        </div>
      )}

      {data && data.recommendations.length > 0 && (
        <div className="grid gap-3 lg:grid-cols-2">
          {data.recommendations.map((rec) => <RecCard key={rec.add.id} rec={rec} />)}
        </div>
      )}
    </div>
  )
}
