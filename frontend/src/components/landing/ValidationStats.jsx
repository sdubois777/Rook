const STATS = [
  { value: '74.1%', label: 'Signal Accuracy', sublabel: 'Overall' },
  { value: '93%', label: 'Buy Accuracy', sublabel: '42 players' },
  { value: '87%', label: 'Top Opportunities', sublabel: '13 of 15' },
  { value: '0.88', label: 'Correlation', sublabel: 'Projected vs actual' },
]

const CALLS = [
  {
    player: 'Jaxon Smith-Njigba',
    signal: 'BUY',
    price: '$32',
    result: 'Top-5 WR · 359 PPR points',
    correct: true,
  },
  {
    player: 'Chris Olave',
    signal: 'BUY',
    price: '$9',
    result: 'WR1 production · 268 PPR points',
    correct: true,
  },
  {
    player: 'Brian Thomas Jr.',
    signal: 'AVOID',
    price: '$51',
    result: 'Just 138 PPR — league overpaid',
    correct: true,
  },
  {
    player: 'Saquon Barkley',
    signal: 'AVOID',
    price: '$61',
    result: '230 PPR — bust at that price',
    correct: true,
  },
]

export default function ValidationStats() {
  return (
    <section id="results" className="py-20 px-4 sm:px-6 bg-gray-900/20">
      <div className="max-w-5xl mx-auto">
        <h2 className="text-3xl sm:text-4xl font-bold text-white text-center mb-2">
          Proven Results
        </h2>
        <p className="text-gray-400 text-center mb-12">
          2025 NFL Season — real backtest, real outcomes.
        </p>

        {/* Stat cards */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-16">
          {STATS.map((s) => (
            <div
              key={s.label}
              className="bg-gray-900/60 border border-gray-800 rounded-xl p-6 text-center"
            >
              <div className="text-3xl sm:text-4xl font-extrabold text-white">
                {s.value}
              </div>
              <div className="text-sm font-medium text-gray-300 mt-1">
                {s.label}
              </div>
              <div className="text-xs text-gray-500 mt-0.5">{s.sublabel}</div>
            </div>
          ))}
        </div>

        {/* Example calls */}
        <div className="grid sm:grid-cols-2 gap-4">
          {CALLS.map((c) => (
            <div
              key={c.player}
              className="flex items-start gap-4 bg-gray-900/40 border border-gray-800 rounded-xl p-5"
            >
              <div
                className={`shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold ${
                  c.correct
                    ? 'bg-green-900/40 text-green-400'
                    : 'bg-red-900/40 text-red-400'
                }`}
              >
                &#10003;
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-white">{c.player}</span>
                  <span
                    className={`text-xs font-bold px-1.5 py-0.5 rounded ${
                      c.signal === 'BUY'
                        ? 'bg-green-900/50 text-green-400'
                        : 'bg-red-900/50 text-red-400'
                    }`}
                  >
                    {c.signal} at {c.price}
                  </span>
                </div>
                <p className="text-sm text-gray-400 mt-1">{c.result}</p>
              </div>
            </div>
          ))}
        </div>

        <p className="text-xs text-gray-600 text-center mt-8">
          Based on 2025 NFL season actual results. Backtest uses pre-2025 data
          to project 2025 outcomes.
        </p>
      </div>
    </section>
  )
}
