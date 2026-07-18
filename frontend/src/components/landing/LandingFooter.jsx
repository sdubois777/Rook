import { Link } from 'react-router-dom'

export default function LandingFooter() {
  return (
    <footer className="border-t border-gray-800 py-6 px-4 sm:px-6">
      <div className="max-w-6xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-4 text-xs text-gray-600">
        <span>&copy; {new Date().getFullYear()} Rook. All rights reserved.</span>
        <div className="flex gap-6">
          <Link to="/pricing" className="hover:text-gray-400 transition-colors">Pricing</Link>
          {/* Plain <a> (full navigation): /terms and /privacy are server-rendered
              static pages, not SPA routes — a <Link> would client-route into the
              catch-all. */}
          <a href="/terms" className="hover:text-gray-400 transition-colors">Terms</a>
          <a href="/privacy" className="hover:text-gray-400 transition-colors">Privacy</a>
          <a href="mailto:support@rookff.com" className="hover:text-gray-400 transition-colors">Contact</a>
        </div>
      </div>
    </footer>
  )
}
