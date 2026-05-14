import { Link } from 'react-router-dom'

const TIERS = [
  {
    name: 'Intro',
    monthly: 5,
    season: 15,
    features: [
      'All player projections + draft board',
      '1 league sync',
      'Injury monitoring',
      'Manager tendencies',
      '25 credits at signup',
    ],
    cta: 'Get Started',
    highlight: false,
  },
  {
    name: 'Standard',
    monthly: 9,
    season: 29,
    features: [
      'Everything in Intro',
      '2 league syncs',
      'Live draft agent',
      'Trade analyzer',
      'Waiver wire agent',
      '75 credits at signup + 20/mo',
    ],
    cta: 'Start Trial',
    highlight: true,
  },
  {
    name: 'Pro',
    monthly: 18,
    season: 49,
    features: [
      'Everything in Standard',
      'Unlimited leagues',
      'Live draft agent (all leagues)',
      'Trade finder',
      '200 credits at signup + 50/mo',
    ],
    cta: 'Start Trial',
    highlight: false,
  },
]

export default function PricingTable({ showHeader = true }) {
  return (
    <section id="pricing" className="py-20 px-4 sm:px-6">
      <div className="max-w-5xl mx-auto">
        {showHeader && (
          <>
            <h2 className="text-3xl sm:text-4xl font-bold text-white text-center mb-4">
              Simple, Transparent Pricing
            </h2>
            <p className="text-gray-400 text-center mb-12 max-w-xl mx-auto">
              All plans include a 7-day free trial. No credit card required to
              start.
            </p>
          </>
        )}

        <div className="grid md:grid-cols-3 gap-6">
          {TIERS.map((tier) => (
            <div
              key={tier.name}
              className={`relative rounded-xl p-8 border transition-colors ${
                tier.highlight
                  ? 'border-blue-500 bg-blue-950/20 shadow-lg shadow-blue-500/5'
                  : 'border-gray-800 bg-gray-900/40 hover:border-gray-700'
              }`}
            >
              {tier.highlight && (
                <span className="absolute -top-3 left-1/2 -translate-x-1/2 bg-blue-600 text-white text-xs font-bold px-3 py-1 rounded-full">
                  Most Popular
                </span>
              )}

              <h3 className="text-lg font-semibold text-white">{tier.name}</h3>

              <div className="mt-4 flex items-baseline gap-1">
                <span className="text-4xl font-extrabold text-white">
                  ${tier.monthly}
                </span>
                <span className="text-gray-400 text-sm">/month</span>
              </div>
              <p className="text-sm text-gray-500 mt-1">
                or ${tier.season}/season
              </p>

              <ul className="mt-6 space-y-3">
                {tier.features.map((f) => (
                  <li key={f} className="flex items-start gap-2 text-sm text-gray-300">
                    <svg className="w-4 h-4 text-green-400 mt-0.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                    </svg>
                    {f}
                  </li>
                ))}
              </ul>

              <Link
                to="/sign-up"
                className={`mt-8 block text-center py-3 rounded-lg font-semibold text-sm transition-colors ${
                  tier.highlight
                    ? 'bg-blue-600 hover:bg-blue-500 text-white'
                    : 'bg-gray-800 hover:bg-gray-700 text-gray-200'
                }`}
              >
                {tier.cta}
              </Link>
            </div>
          ))}
        </div>

        {/* Credit info */}
        <div className="mt-12 text-center text-sm text-gray-500 max-w-2xl mx-auto space-y-2">
          <p>
            <span className="text-gray-400">Free always:</span> browsing
            projections, draft board, news, injury monitoring.
          </p>
          <p>
            <span className="text-gray-400">Credit costs:</span> Trade analysis
            10cr &middot; Waiver wire 8cr/week &middot; Trade finder 20cr (Pro).
          </p>
          <p>
            <span className="text-gray-400">Need more?</span> $5 = 75cr
            &middot; $10 = 175cr &middot; $25 = 500cr. Credits never expire.
          </p>
        </div>
      </div>
    </section>
  )
}
