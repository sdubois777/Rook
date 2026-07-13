import { Link } from 'react-router-dom'
import { TrendingUp, Minus, ArrowRight } from 'lucide-react'
import { usePricing } from '../../hooks/usePricing'

/**
 * Hero — leads with the PRODUCT, not a mascot. The signature element is a
 * faithful, static render of the app's real trade VerdictPanel showing a real
 * engine result (The Lord ↔ Joe Shiesty, 2025-season data — see
 * docs/trade_output_review.md, a reproducible network-free dump). Same chrome,
 * type, and mono/tabular numbers as the in-app panel, so the landing page and
 * the app read as the same product. Numbers are NOT fabricated.
 */

// --- real verdict data (verbatim trade values from the engine dump) ----------
const GIVE = [{ name: 'Tyler Warren', pos: 'TE', value: 32, trend: 'stable' }]
const GET = [
  { name: 'Zay Flowers', pos: 'WR', value: 49, trend: 'rising', buyLow: true },
  { name: 'Terry McLaurin', pos: 'WR', value: 42, trend: 'stable' },
]

function ValueRow({ p }) {
  const Trend = p.trend === 'rising' ? TrendingUp : Minus
  const trendCls = p.trend === 'rising' ? 'text-emerald-400' : 'text-slate-500'
  return (
    <div className="flex items-center justify-between gap-3 rounded-md bg-surface-2 px-3 py-2">
      <div className="flex items-center gap-2 min-w-0">
        <span className="rounded bg-surface-3 px-1.5 py-0.5 text-[10px] font-semibold text-slate-400">
          {p.pos}
        </span>
        <span className="truncate text-sm font-medium text-white">{p.name}</span>
        {p.buyLow && (
          <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[9px] font-semibold text-emerald-400">
            BUY-LOW
          </span>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className="font-mono tabular-nums text-sm font-semibold text-white">
          {p.value}
        </span>
        <Trend size={14} className={trendCls} />
      </div>
    </div>
  )
}

/** A static replica of the in-app VerdictPanel — the hero's one bold element. */
function VerdictCard() {
  return (
    <div className="w-full max-w-md rounded-xl border border-border bg-surface-1 shadow-2xl shadow-brand/20">
      {/* Verdict header — brand-accent, exactly like a confident in-app verdict */}
      <div className="rounded-t-xl border-b border-brand-accent/40 bg-brand/10 px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <span className="text-base font-semibold text-white">You win</span>
            <span className="text-sm text-brand-accent">· fair trade</span>
          </div>
          <div className="text-right text-xs text-slate-400">
            <div className="font-mono tabular-nums">
              your lineup <span className="text-emerald-400">+7.8 pts/wk</span>
            </div>
          </div>
        </div>
        <div className="mt-1 text-[11px] text-slate-500">
          confidence: <span className="text-slate-300">full</span>
        </div>
      </div>

      <div className="space-y-3 p-4">
        <p className="text-sm text-slate-300">
          One tight end for two starting receivers. Zay Flowers grades{' '}
          <span className="text-emerald-400">buy-low</span> — Rook sets his trade
          value from in-season usage, not preseason rank.
        </p>

        {/* Give/get stacked (not two narrow columns) so player names read in
            full at hero width — no truncation on the showpiece. */}
        <div className="space-y-3">
          <div>
            <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
              You give
            </div>
            <div className="space-y-1.5">
              {GIVE.map((p) => <ValueRow key={p.name} p={p} />)}
            </div>
          </div>
          <div>
            <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
              You get
            </div>
            <div className="space-y-1.5">
              {GET.map((p) => <ValueRow key={p.name} p={p} />)}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export default function Hero() {
  const { tierById } = usePricing()
  // Credits derived from the pricing sheet (user.py) — never hardcoded.
  const freeCredits = tierById?.free?.credits_signup_bonus

  return (
    <section className="relative overflow-hidden px-4 pt-28 pb-16 sm:px-6 lg:pt-36 lg:pb-24">
      {/* Soft navy glow behind the product — atmosphere, not a flat wash */}
      <div
        aria-hidden
        className="pointer-events-none absolute -top-24 right-0 h-[36rem] w-[36rem] rounded-full bg-brand/20 blur-[120px] lg:right-10"
      />

      <div className="relative mx-auto grid max-w-6xl items-center gap-12 lg:grid-cols-[1.05fr_1fr] lg:gap-8">
        {/* LEFT — the thesis, tight vertical rhythm */}
        <div className="motion-safe:animate-[fadeUp_0.5s_ease-out]">
          <span className="inline-flex items-center gap-2 rounded-full border border-border bg-surface-1 px-3 py-1 text-xs font-medium text-brand-accent">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
            In-season trade &amp; draft engine
          </span>

          <h1 className="mt-5 text-4xl font-bold leading-[1.1] tracking-tight text-slate-100 sm:text-5xl">
            Fantasy values that reason about{' '}
            <span className="text-brand-accent">why</span>.
          </h1>

          <p className="mt-4 max-w-lg text-lg leading-relaxed text-slate-400">
            Rook builds every player&apos;s value from in-season usage and the
            causes behind it — then shows you the trades, waivers, and draft picks
            your league is mispricing.
          </p>

          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center">
            <Link
              to="/sign-up"
              className="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-brand px-7 py-3 font-semibold text-white transition-colors hover:bg-brand-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface-0"
            >
              Start free <ArrowRight size={18} />
            </Link>
            <a
              href="#how-it-works"
              className="inline-flex min-h-11 items-center justify-center rounded-lg border border-border px-7 py-3 font-medium text-slate-300 transition-colors hover:border-slate-500 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-accent"
            >
              See how it works
            </a>
          </div>

          <p className="mt-5 text-sm text-slate-500">
            Free forever{freeCredits ? ` · ${freeCredits} credits to start` : ''} ·
            no card required. Works with Yahoo, ESPN, and Sleeper.
          </p>
        </div>

        {/* RIGHT — the product being right (the one bold element) */}
        <div className="flex flex-col items-center gap-3 lg:items-end motion-safe:animate-[fadeUp_0.6s_ease-out]">
          <VerdictCard />
          <p className="max-w-md text-center text-xs text-slate-600 lg:text-right">
            A real verdict from Rook&apos;s value engine — 2025 season data.
          </p>
        </div>
      </div>

      {/* Page-load reveal; respects reduced-motion (motion-safe only) */}
      <style>{`
        @keyframes fadeUp {
          from { opacity: 0; transform: translateY(12px); }
          to { opacity: 1; transform: translateY(0); }
        }
      `}</style>
    </section>
  )
}
