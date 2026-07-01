/**
 * SilenceExplainer — the first-class empty state for "Trade ideas".
 *
 * When 0 trades surface, this explains WHY in plain language (the honest,
 * measured reason from the backend) instead of a bare "nothing found" — and, if
 * one exists, shows the closest NEAR-MISS as a negotiation starting point. The
 * near-miss is styled + labeled so it can NEVER be mistaken for an endorsed
 * recommendation.
 */
import { Minus, Handshake } from 'lucide-react'

function NearMissCard({ nm }) {
  const names = (list) => (list || []).map((p) => p.name).join(' + ')
  return (
    <div className="rounded-lg border border-dashed border-slate-600/60 bg-surface-2/40 px-4 py-3 text-left">
      <div className="flex items-center gap-2">
        <Handshake size={15} className="shrink-0 text-slate-400" />
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
          Closest possible deal · would take some convincing
        </span>
      </div>
      <div className="mt-2 text-sm text-slate-300">
        <span className="text-slate-500">You give</span> {names(nm.give)}{' '}
        <span className="text-slate-500">for</span> {names(nm.get)}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-slate-500">
        <span>
          would add <span className="text-slate-300 tabular-nums">+{nm.would_be_ppg}</span> pts/wk to your lineup
        </span>
        <span>·</span>
        <span>{nm.shortfall_reason}</span>
      </div>
      <div className="mt-1.5 text-[11px] text-slate-600">
        Not a recommendation — just the nearest thing on the board.
      </div>
    </div>
  )
}

export default function SilenceExplainer({ context, fallbackMessage }) {
  // Nothing to say (e.g. trades DID surface, so the parent shouldn't render this).
  if (!context && !fallbackMessage) return null
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-border bg-surface-1 px-4 py-6 text-center text-slate-300">
        <Minus size={22} className="mx-auto mb-1 text-slate-600" />
        <div className="text-sm">
          {context?.message || fallbackMessage || 'No clear trade right now.'}
        </div>
        <div className="mt-1 text-xs text-slate-600">
          That's a real answer, not a miss — nothing on the board clears the bar.
        </div>
      </div>

      {context?.near_miss && <NearMissCard nm={context.near_miss} />}
    </div>
  )
}
