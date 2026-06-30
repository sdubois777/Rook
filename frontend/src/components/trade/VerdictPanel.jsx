/**
 * VerdictPanel — renders a trade verdict EXACTLY as the backend returned it.
 *
 * The payoff: the panel reads winner AND hedged AND confidence — never winner
 * alone. A hedged / limited / insufficient verdict is visually MUTED (amber,
 * "tentative" caveat, hedge_reason surfaced) so a team-change or thin-data trade
 * reads as "lean, with a caveat", not a confident win. The UI never re-derives
 * or rounds a verdict — the deterministic backend value is the source of truth.
 */
import { TrendingUp, TrendingDown, Minus, AlertTriangle, ShieldAlert, Handshake, XCircle, Scale } from 'lucide-react'

const TREND = {
  rising: { Icon: TrendingUp, cls: 'text-emerald-400', label: 'rising' },
  falling: { Icon: TrendingDown, cls: 'text-red-400', label: 'falling' },
  stable: { Icon: Minus, cls: 'text-slate-400', label: 'stable' },
}

const CONF_TEXT = {
  full: 'text-slate-300',
  limited: 'text-amber-400',
  insufficient: 'text-slate-500',
}

function PlayerRow({ p }) {
  const t = TREND[p.value_trend] || TREND.stable
  return (
    <div className="flex items-start justify-between gap-3 rounded-md bg-surface-2 px-3 py-2">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-medium text-white truncate">{p.name}</span>
          <span className="text-xs text-slate-400">{p.position}</span>
          {p.buy_low && (
            <span className="rounded bg-emerald-500/15 text-emerald-400 text-[10px] font-semibold px-1.5 py-0.5">BUY-LOW</span>
          )}
          {p.sell_high && (
            <span className="rounded bg-amber-500/15 text-amber-400 text-[10px] font-semibold px-1.5 py-0.5">SELL-HIGH</span>
          )}
        </div>
        {p.why && <div className="mt-0.5 text-xs text-slate-500 line-clamp-2">{p.why}</div>}
      </div>
      <div className="flex shrink-0 items-center gap-2.5 text-sm">
        <span className="tabular-nums font-semibold text-white" title="forward value (0-100)">
          {Math.round(p.forward_value)}
        </span>
        <t.Icon size={15} className={t.cls} />
        <span className={`text-[11px] ${CONF_TEXT[p.confidence] || 'text-slate-400'}`}>{p.confidence}</span>
      </div>
    </div>
  )
}

function winnerLabel(v) {
  if (v.winner === 'you') return 'You win'
  if (v.winner === 'opponent') return 'You lose'
  return 'Even trade'
}

// Acceptability styling — the OTHER side's read. A "likely_reject" must read as a
// clear caution (NOT a win), even when the your-side verdict above says "You win".
const ACCEPT = {
  likely_accept: {
    Icon: Handshake, label: 'Likely to accept',
    box: 'border-emerald-500/30 bg-emerald-500/[0.06]',
    title: 'text-emerald-300', body: 'text-emerald-200/90',
  },
  marginal: {
    Icon: Scale, label: 'May haggle',
    box: 'border-amber-500/30 bg-amber-500/[0.06]',
    title: 'text-amber-300', body: 'text-amber-200/90',
  },
  likely_reject: {
    Icon: XCircle, label: 'Likely to reject',
    box: 'border-red-500/40 bg-red-500/[0.07]',
    title: 'text-red-300', body: 'text-red-200/90',
  },
}

function AcceptabilitySection({ a }) {
  if (!a) return null
  const s = ACCEPT[a.verdict] || ACCEPT.marginal
  const gain = a.their_lineup_gain
  const netSign = gain > 0 ? '+' : ''
  return (
    <div className={`rounded-md border px-3 py-2.5 ${s.box}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <s.Icon size={16} className={`shrink-0 ${s.title}`} />
          <span className={`text-sm font-semibold ${s.title}`}>{s.label}</span>
          <span className="text-[10px] uppercase tracking-wide text-slate-500">other side</span>
          {a.hedged && (
            <span className="rounded-full bg-amber-500/15 text-amber-300 text-[10px] font-semibold uppercase tracking-wide px-2 py-0.5">
              tentative
            </span>
          )}
        </div>
        <div className="text-right text-xs text-slate-400 tabular-nums">
          <span className="text-slate-300">their lineup </span>
          <span className={gain > 0 ? 'text-emerald-400' : 'text-red-400'}>
            {netSign}{gain} pts/wk
          </span>
        </div>
      </div>
      {a.why && <div className={`mt-1 text-xs ${s.body}`}>{a.why}</div>}
      {a.overtake_flag && (
        <div className="mt-1.5 flex items-start gap-1.5 text-xs text-red-300/90">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
          <span>Helps them more on the field — this could let their lineup overtake yours.</span>
        </div>
      )}
    </div>
  )
}

export default function VerdictPanel({ verdict: v, className = '' }) {
  if (!v) return null
  // Confidence-aware: only a clean, full-confidence verdict reads as confident.
  const confident = !v.hedged && v.confidence === 'full' && v.winner !== 'even'

  const headerCls = confident
    ? 'border-brand-accent/40 bg-brand/10'
    : 'border-amber-500/40 bg-amber-500/[0.06]'

  const gain = v.lineup_gain ?? 0
  const gainSign = gain > 0 ? '+' : ''

  return (
    <div className={`rounded-lg border border-border bg-surface-1 ${className}`}>
      {/* Verdict header — styled by confidence, not winner alone */}
      <div className={`rounded-t-lg border-b px-4 py-3 ${headerCls}`}>
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            {!confident && <AlertTriangle size={16} className="text-amber-400 shrink-0" />}
            <span className={`text-base font-semibold ${confident ? 'text-white' : 'text-amber-200'}`}>
              {winnerLabel(v)}
            </span>
            <span className={`text-sm ${confident ? 'text-brand-accent' : 'text-amber-300/80'}`}>
              · {v.fairness}
            </span>
            {!confident && (
              <span className="rounded-full bg-amber-500/15 text-amber-300 text-[10px] font-semibold uppercase tracking-wide px-2 py-0.5">
                tentative
              </span>
            )}
          </div>
          <div className="text-right text-xs text-slate-400">
            <div className="tabular-nums">
              <span className="text-slate-300">your lineup </span>
              <span className={gain >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                {gainSign}{gain} pts/wk
              </span>
            </div>
            <div className="tabular-nums">get {v.get_value} · give {v.give_value}</div>
          </div>
        </div>

        {/* Hedge reason is surfaced, never buried */}
        {v.hedged && v.hedge_reason && (
          <div className="mt-2 flex items-start gap-1.5 text-xs text-amber-300/90">
            <span className="font-semibold shrink-0">Caveat:</span>
            <span>{v.hedge_reason}</span>
          </div>
        )}
        <div className="mt-1 text-[11px] text-slate-500">
          confidence: <span className={CONF_TEXT[v.confidence]}>{v.confidence}</span>
        </div>
      </div>

      <div className="p-4 space-y-3">
        {v.rationale && <p className="text-sm text-slate-300">{v.rationale}</p>}

        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">You give</div>
            <div className="space-y-1.5">
              {v.give.map((p) => <PlayerRow key={p.id} p={p} />)}
            </div>
          </div>
          <div>
            <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">You get</div>
            <div className="space-y-1.5">
              {v.get.map((p) => <PlayerRow key={p.id} p={p} />)}
            </div>
          </div>
        </div>

        {/* Acceptability — the OTHER side's read, visually distinct from the
            your-side verdict above. "Likely to reject" never reads as a win. */}
        <AcceptabilitySection a={v.acceptability} />

        {/* Roster guard — only when triggered */}
        {v.roster_guard?.triggered && (
          <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/[0.06] px-3 py-2">
            <ShieldAlert size={16} className="mt-0.5 shrink-0 text-amber-400" />
            <div className="text-xs text-amber-200/90">
              <div>{v.roster_guard.message}</div>
              {v.roster_guard.drop_recommendations?.length > 0 && (
                <div className="mt-1 text-amber-300/80">
                  Suggested drop{v.roster_guard.drop_recommendations.length > 1 ? 's' : ''}:{' '}
                  {v.roster_guard.drop_recommendations.map((d) => d.name).join(', ')}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
