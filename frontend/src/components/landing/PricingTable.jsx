import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '@clerk/clerk-react'
import { createCheckout, redirectTo } from '../../api/billing'
import { usePricing } from '../../hooks/usePricing'

/**
 * Pricing table — every dollar amount, credit cost, grant, and pack size is
 * FETCHED from /billing/pricing (served from backend/models/user.py, the single
 * source of truth). Only the marketing feature COPY lives here; never numbers.
 */

// Marketing copy per tier — text only, no prices/credits (those render from
// the fetched pricing sheet).
const TIER_COPY = {
  free: {
    features: [
      'All player values + draft board',
      'Waiver wire browse + start/sit',
      '1 league sync',
      'Injury monitoring',
      'AI features metered by credits',
    ],
    cta: 'Get Started',
    highlight: false,
  },
  standard: {
    features: [
      'Everything in Free — unlimited, no credits',
      'Unlimited trade analyzer + trade finder',
      'Unlimited waiver recommendations',
      'Live draft assistant',
      '1 league sync',
    ],
    cta: 'Start Standard',
    highlight: true,
  },
  pro: {
    features: [
      'Everything in Standard',
      'Unlimited leagues',
      'Cross-league view',
      'Live draft assistant (all leagues)',
    ],
    cta: 'Start Pro',
    highlight: false,
  },
}

export default function PricingTable({ showHeader = true }) {
  const { isSignedIn } = useAuth()
  const { tiers, packs, creditCost, isLoading } = usePricing()
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState('')

  const startCheckout = async (tierId, interval) => {
    setBusy(`${tierId}:${interval}`)
    setError('')
    try {
      redirectTo(await createCheckout(tierId, interval))
    } catch {
      setError('Could not start checkout. Please try again.')
      setBusy(null)
    }
  }

  if (isLoading) {
    return (
      <section id="pricing" className="py-20 px-4 sm:px-6">
        <p className="text-center text-gray-500">Loading pricing…</p>
      </section>
    )
  }

  const pack = packs[0]

  return (
    <section id="pricing" className="py-20 px-4 sm:px-6">
      <div className="max-w-5xl mx-auto">
        {showHeader && (
          <>
            <h2 className="text-3xl sm:text-4xl font-bold text-white text-center mb-4">
              Simple, Transparent Pricing
            </h2>
            <p className="text-gray-400 text-center mb-12 max-w-xl mx-auto">
              Start free. Paid plans are unlimited — one price, no credits.
            </p>
          </>
        )}

        <div className="grid md:grid-cols-3 gap-6">
          {tiers.map((tier) => {
            const copy = TIER_COPY[tier.id] || { features: [], cta: 'Start' }
            const isFree = tier.price_monthly_usd === 0
            return (
              <div
                key={tier.id}
                className={`relative rounded-xl p-8 border transition-colors ${
                  copy.highlight
                    ? 'border-brand bg-brand/10 shadow-lg shadow-brand/10'
                    : 'border-gray-800 bg-gray-900/40 hover:border-gray-700'
                }`}
              >
                {copy.highlight && (
                  <span className="absolute -top-3 left-1/2 -translate-x-1/2 bg-brand text-white text-xs font-bold px-3 py-1 rounded-full">
                    Most Popular
                  </span>
                )}

                <h3 className="text-lg font-semibold text-white">{tier.label}</h3>

                <div className="mt-4 flex items-baseline gap-1">
                  <span className="text-4xl font-extrabold text-white">
                    ${tier.price_monthly_usd}
                  </span>
                  <span className="text-gray-400 text-sm">/month</span>
                </div>
                <p className="text-sm text-gray-500 mt-1">
                  {isFree
                    ? `${tier.credits_signup_bonus} credits at signup`
                    : `or $${tier.price_season_usd}/season`}
                </p>

                <ul className="mt-6 space-y-3">
                  {copy.features.map((f) => (
                    <li key={f} className="flex items-start gap-2 text-sm text-gray-300">
                      <svg className="w-4 h-4 text-green-400 mt-0.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                      </svg>
                      {f}
                    </li>
                  ))}
                </ul>

                {isFree || !isSignedIn ? (
                  <Link
                    to="/sign-up"
                    className={`mt-8 block text-center py-3 rounded-lg font-semibold text-sm transition-colors ${
                      copy.highlight
                        ? 'bg-brand hover:bg-brand-hover text-white'
                        : 'bg-gray-800 hover:bg-gray-700 text-gray-200'
                    }`}
                  >
                    {copy.cta}
                  </Link>
                ) : (
                  <div className="mt-8 space-y-2">
                    <button
                      onClick={() => startCheckout(tier.id, 'monthly')}
                      disabled={busy !== null}
                      className={`block w-full text-center py-3 rounded-lg font-semibold text-sm transition-colors disabled:opacity-50 ${
                        copy.highlight
                          ? 'bg-brand hover:bg-brand-hover text-white'
                          : 'bg-gray-800 hover:bg-gray-700 text-gray-200'
                      }`}
                    >
                      {busy === `${tier.id}:monthly`
                        ? 'Redirecting…'
                        : `Monthly — $${tier.price_monthly_usd}/mo`}
                    </button>
                    <button
                      onClick={() => startCheckout(tier.id, 'season')}
                      disabled={busy !== null}
                      className="block w-full text-center py-2.5 rounded-lg font-semibold text-sm transition-colors disabled:opacity-50 border border-gray-700 text-gray-200 hover:border-gray-500"
                    >
                      {busy === `${tier.id}:season`
                        ? 'Redirecting…'
                        : `Season pass — $${tier.price_season_usd}`}
                    </button>
                  </div>
                )}
              </div>
            )
          })}
        </div>

        {error && (
          <p className="mt-6 text-center text-sm text-red-400">{error}</p>
        )}

        {/* Credit info — rendered from the fetched sheet, never hardcoded */}
        <div className="mt-12 text-center text-sm text-gray-500 max-w-2xl mx-auto space-y-2">
          <p>
            <span className="text-gray-400">Always free, every plan:</span>{' '}
            player values, teams, waiver wire browse, start/sit, injury
            monitoring.
          </p>
          <p>
            <span className="text-gray-400">Free-plan credit costs:</span>{' '}
            Trade analysis {creditCost('trade_analysis')}cr &middot; Waiver
            recommendations {creditCost('waiver_wire')}cr &middot; Trade finder{' '}
            {creditCost('trade_finder')}cr.
          </p>
          {pack && (
            <p>
              <span className="text-gray-400">Need more?</span> ${pack.price_usd}{' '}
              = {pack.credits}cr. Credits never expire.
            </p>
          )}
        </div>
      </div>
    </section>
  )
}
