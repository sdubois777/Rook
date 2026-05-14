import { Link } from 'react-router-dom'
import { SignedIn, SignedOut } from '@clerk/clerk-react'

export default function LandingNav() {
  return (
    <nav className="fixed top-0 left-0 right-0 z-50 bg-[#0f1117]/80 backdrop-blur-md border-b border-gray-800/50">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
        <Link to="/" className="text-xl font-bold text-white tracking-tight">
          Draft<span className="text-blue-400">Mind</span>
        </Link>

        <div className="hidden sm:flex items-center gap-6 text-sm text-gray-400">
          <a href="#how-it-works" className="hover:text-white transition-colors">How It Works</a>
          <a href="#results" className="hover:text-white transition-colors">Results</a>
          <Link to="/pricing" className="hover:text-white transition-colors">Pricing</Link>
        </div>

        <div className="flex items-center gap-3">
          <SignedOut>
            <Link
              to="/sign-in"
              className="text-sm text-gray-300 hover:text-white transition-colors"
            >
              Sign In
            </Link>
            <Link
              to="/sign-up"
              className="text-sm bg-blue-600 hover:bg-blue-500 text-white px-4 py-2 rounded-lg transition-colors"
            >
              Get Started
            </Link>
          </SignedOut>
          <SignedIn>
            <Link
              to="/dashboard"
              className="text-sm bg-blue-600 hover:bg-blue-500 text-white px-4 py-2 rounded-lg transition-colors"
            >
              Dashboard
            </Link>
          </SignedIn>
        </div>
      </div>
    </nav>
  )
}
