import { Link } from 'react-router-dom'

const PROOF_POINTS = [
  '74.1% signal accuracy in 2025 backtesting',
  '93% buy signal accuracy',
  'Works with Yahoo, ESPN, and Sleeper',
]

export default function Hero() {
  return (
    <section className="relative pt-32 pb-20 px-4 sm:px-6 overflow-hidden">
      {/* Background gradient */}
      <div className="absolute inset-0 bg-gradient-to-b from-blue-950/20 via-surface-0 to-surface-0" />

      <div className="relative max-w-4xl mx-auto text-center">
        <img
          src="/rook-mascot.png"
          alt="Rook — the AI fantasy draft rookie mascot"
          width="160"
          height="160"
          className="mx-auto mb-8 w-28 sm:w-32 lg:w-36 h-auto drop-shadow-[0_10px_28px_rgba(42,61,143,0.5)]"
        />
        <h1 className="text-4xl sm:text-5xl lg:text-6xl font-extrabold text-white leading-tight tracking-tight">
          Win Your Fantasy League{' '}
          <span className="text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-cyan-300">
            With AI
          </span>
        </h1>

        <p className="mt-6 text-lg sm:text-xl text-gray-400 max-w-2xl mx-auto leading-relaxed">
          The only fantasy tool that reasons about{' '}
          <span className="text-gray-200 font-medium">why</span> players are
          undervalued — not just what the consensus says.
        </p>

        <div className="mt-10 flex flex-col sm:flex-row items-center justify-center gap-4">
          <Link
            to="/sign-up"
            className="w-full sm:w-auto px-8 py-3.5 bg-brand hover:bg-brand-hover text-white font-semibold rounded-lg transition-colors text-center"
          >
            Start Free &rarr;
          </Link>
          <a
            href="#how-it-works"
            className="w-full sm:w-auto px-8 py-3.5 border border-gray-700 hover:border-gray-500 text-gray-300 hover:text-white rounded-lg transition-colors text-center"
          >
            See How It Works
          </a>
        </div>

        <div className="mt-12 flex flex-col sm:flex-row items-center justify-center gap-4 sm:gap-8 text-sm text-gray-400">
          {PROOF_POINTS.map((point) => (
            <div key={point} className="flex items-center gap-2">
              <svg className="w-4 h-4 text-green-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
              <span>{point}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
