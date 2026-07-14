import { Link } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import { usePricing } from '../../hooks/usePricing'

/**
 * Hero — leads with the PRODUCT, not a mascot. The signature element is a
 * faithful, static render of a real player card from the app's in-season value
 * engine. A single player + the engine's OWN reasoning PROVES the "reason about
 * why" headline (and, being an explanation rather than a trade recommendation,
 * there's no "that's a robbery" reaction to have).
 *
 * EVERY number and the `why` sentence below are verbatim engine output for
 * Ashton Jeanty at 2025 season week 14 (evaluate_league over the demo league;
 * the value engine consumes REAL 2025 per-week usage from PBP). Cross-checkable:
 * a visitor can verify Jeanty's real 2025 usage/production. Nothing is invented.
 */

// --- real engine output (Ashton Jeanty, 2025 wk14 — see commit message) ------
const CARD = {
  name: 'Ashton Jeanty',
  pos: 'RB',
  team: 'Las Vegas Raiders',
  games: 13,
  value: '55.7', // forward_value (0-100, position-relative)
  confidence: 'full',
  // verbatim InSeasonValue.why
  why: 'producing below volume (11 vs ~17 expected) — buy-low',
  usagePrior: '50%', // usage_prior
  usageRecent: '54%', // usage_recent
  scoring: '11.5', // recency_ppg
  expected: '17.0', // expected_ppg
}

/** A static player card in the app's own chrome — the hero's one bold element. */
function PlayerCard() {
  return (
    <div className="w-full max-w-md rounded-xl border border-border bg-surface-1 shadow-2xl shadow-brand/20">
      {/* Header — player identity left, the headline value right */}
      <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded bg-surface-3 px-1.5 py-0.5 text-[10px] font-semibold text-slate-400">
              {CARD.pos}
            </span>
            <span className="text-base font-semibold text-white">{CARD.name}</span>
            <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[9px] font-semibold text-emerald-400">
              BUY-LOW
            </span>
          </div>
          <div className="mt-0.5 text-xs text-slate-500">
            {CARD.team} · {CARD.games} games
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="text-[10px] uppercase tracking-wide text-slate-500">
            Rook value
          </div>
          <div className="font-mono tabular-nums text-2xl font-bold leading-tight text-white">
            {CARD.value}
          </div>
        </div>
      </div>

      <div className="space-y-3 p-4">
        {/* The engine's OWN reasoning — the whole point of the card */}
        <div>
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
            Why Rook flags it
          </div>
          <p className="text-sm text-slate-300">{CARD.why}</p>
        </div>

        {/* Real supporting metrics — labels only; every number is engine output */}
        <div className="grid grid-cols-3 gap-2">
          <Metric label="Usage">
            <span className="font-mono tabular-nums">
              {CARD.usagePrior}
              <span className="text-slate-500"> → </span>
              <span className="text-emerald-400">{CARD.usageRecent}</span>
            </span>
          </Metric>
          <Metric label="Scoring">
            <span className="font-mono tabular-nums">{CARD.scoring}</span>
            <span className="text-xs text-slate-500"> pg</span>
          </Metric>
          <Metric label="Its usage supports">
            <span className="font-mono tabular-nums">{CARD.expected}</span>
            <span className="text-xs text-slate-500"> pg</span>
          </Metric>
        </div>

        <div className="text-[11px] text-slate-500">
          confidence: <span className="text-slate-300">{CARD.confidence}</span>
        </div>
      </div>
    </div>
  )
}

function Metric({ label, children }) {
  return (
    <div className="rounded-md bg-surface-2 px-2.5 py-2">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-0.5 text-sm font-medium text-white">{children}</div>
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
          <PlayerCard />
          <p className="max-w-md text-center text-xs text-slate-600 lg:text-right">
            A real player card from Rook&apos;s value engine — 2025 season, through Week 14.
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
