const QUOTES = [
  {
    highlight: 'Jaxon Smith-Njigba',
    text: 'was flagged as undervalued at $32. He finished as a top-5 WR with 359 PPR points.',
    signal: 'BUY',
  },
  {
    highlight: 'Brian Thomas Jr.',
    text: 'was flagged as an overpay at $51. He finished with just 138 PPR points.',
    signal: 'AVOID',
  },
]

export default function SocialProof() {
  return (
    <section className="py-12 border-y border-gray-800/50 bg-gray-900/30">
      <div className="max-w-5xl mx-auto px-4 sm:px-6">
        <div className="grid sm:grid-cols-2 gap-6">
          {QUOTES.map((q) => (
            <blockquote
              key={q.highlight}
              className="relative bg-gray-900/60 border border-gray-800 rounded-xl p-6"
            >
              <span
                className={`absolute top-4 right-4 text-xs font-bold px-2 py-0.5 rounded ${
                  q.signal === 'BUY'
                    ? 'bg-green-900/50 text-green-400'
                    : 'bg-red-900/50 text-red-400'
                }`}
              >
                {q.signal}
              </span>
              <p className="text-gray-300 leading-relaxed">
                &ldquo;<span className="text-white font-semibold">{q.highlight}</span>{' '}
                {q.text}&rdquo;
              </p>
              <cite className="block mt-3 text-xs text-gray-500 not-italic">
                2025 NFL Season — AI Backtest Result
              </cite>
            </blockquote>
          ))}
        </div>
      </div>
    </section>
  )
}
