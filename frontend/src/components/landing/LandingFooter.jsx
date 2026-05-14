import { Link } from 'react-router-dom'

export default function LandingFooter() {
  return (
    <footer className="border-t border-gray-800">
      {/* Final CTA */}
      <div className="py-16 px-4 sm:px-6 text-center">
        <h2 className="text-2xl sm:text-3xl font-bold text-white mb-4">
          Ready to stop guessing?
        </h2>
        <p className="text-gray-400 mb-8 max-w-lg mx-auto">
          Start your free trial — no credit card required. 25 free credits.
          Browse every player projection. Connect your league when you&apos;re
          ready.
        </p>
        <Link
          to="/sign-up"
          className="inline-block px-8 py-3.5 bg-blue-600 hover:bg-blue-500 text-white font-semibold rounded-lg transition-colors"
        >
          Create Free Account &rarr;
        </Link>
      </div>

      {/* Bottom bar */}
      <div className="border-t border-gray-800/50 py-6 px-4 sm:px-6">
        <div className="max-w-6xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-4 text-xs text-gray-600">
          <span>&copy; {new Date().getFullYear()} DraftMind. All rights reserved.</span>
          <div className="flex gap-6">
            <Link to="/pricing" className="hover:text-gray-400 transition-colors">Pricing</Link>
            <a href="mailto:support@draftmind.app" className="hover:text-gray-400 transition-colors">Contact</a>
          </div>
        </div>
      </div>
    </footer>
  )
}
