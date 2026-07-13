import { Link } from 'react-router-dom'
import { usePricing } from '../../hooks/usePricing'

export default function LandingFooter() {
  const { tierById } = usePricing()
  // Credits derived from the pricing sheet (user.py) — never hardcoded. Rook has
  // a permanent free tier (nothing expires), so the CTA never implies a clock.
  const freeCredits = tierById?.free?.credits_signup_bonus

  return (
    <footer className="border-t border-gray-800">
      {/* Final CTA */}
      <div className="py-16 px-4 sm:px-6 text-center">
        <h2 className="text-2xl sm:text-3xl font-bold text-white mb-4">
          Ready to stop guessing?
        </h2>
        <p className="text-gray-400 mb-8 max-w-lg mx-auto">
          Free forever — no credit card{freeCredits ? `, ${freeCredits} credits to start` : ''}.
          Browse every player value. Connect your league when you&apos;re ready.
        </p>
        <Link
          to="/sign-up"
          className="inline-block px-8 py-3.5 bg-brand hover:bg-brand-hover text-white font-semibold rounded-lg transition-colors"
        >
          Create Free Account &rarr;
        </Link>
      </div>

      {/* Bottom bar */}
      <div className="border-t border-gray-800/50 py-6 px-4 sm:px-6">
        <div className="max-w-6xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-4 text-xs text-gray-600">
          <span>&copy; {new Date().getFullYear()} Rook. All rights reserved.</span>
          <div className="flex gap-6">
            <Link to="/pricing" className="hover:text-gray-400 transition-colors">Pricing</Link>
            {/* Plain <a> (full navigation): /privacy is a server-rendered static page,
                not an SPA route — a <Link> would client-route into the catch-all.
                Room for a /terms link here once it exists. */}
            <a href="/privacy" className="hover:text-gray-400 transition-colors">Privacy</a>
            <a href="mailto:support@rookff.com" className="hover:text-gray-400 transition-colors">Contact</a>
          </div>
        </div>
      </div>
    </footer>
  )
}
