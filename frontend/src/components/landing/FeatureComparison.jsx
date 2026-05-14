const FEATURES = [
  { name: 'AI causal reasoning', us: true, fp: false, ud: false },
  { name: 'League-specific history', us: true, fp: false, ud: false },
  { name: 'Opponent tendency analysis', us: true, fp: false, ud: false },
  { name: 'Live draft agent', us: true, fp: false, ud: false },
  { name: 'Yahoo / ESPN / Sleeper', us: true, fp: true, ud: true },
  { name: 'Auction support', us: true, fp: true, ud: true },
  { name: 'Snake draft support', us: true, fp: true, ud: true },
]

function Check() {
  return (
    <svg className="w-5 h-5 text-green-400 mx-auto" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
    </svg>
  )
}

function Cross() {
  return (
    <svg className="w-5 h-5 text-gray-600 mx-auto" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
    </svg>
  )
}

export default function FeatureComparison() {
  return (
    <section className="py-20 px-4 sm:px-6">
      <div className="max-w-4xl mx-auto">
        <h2 className="text-3xl sm:text-4xl font-bold text-white text-center mb-4">
          Why DraftMind?
        </h2>
        <p className="text-gray-400 text-center mb-12 max-w-xl mx-auto">
          Every tool shows you the same consensus. DraftMind shows you what the
          consensus is missing.
        </p>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-800">
                <th className="text-left py-3 px-4 text-gray-400 font-medium" />
                <th className="py-3 px-4 text-center text-white font-semibold">
                  DraftMind
                </th>
                <th className="py-3 px-4 text-center text-gray-400 font-medium">
                  FantasyPros
                </th>
                <th className="py-3 px-4 text-center text-gray-400 font-medium">
                  Underdog
                </th>
              </tr>
            </thead>
            <tbody>
              {FEATURES.map((f, i) => (
                <tr
                  key={f.name}
                  className={`border-b border-gray-800/50 ${
                    i < 4 ? 'bg-blue-950/10' : ''
                  }`}
                >
                  <td className="py-3 px-4 text-gray-300">{f.name}</td>
                  <td className="py-3 px-4 text-center">
                    {f.us ? <Check /> : <Cross />}
                  </td>
                  <td className="py-3 px-4 text-center">
                    {f.fp ? <Check /> : <Cross />}
                  </td>
                  <td className="py-3 px-4 text-center">
                    {f.ud ? <Check /> : <Cross />}
                  </td>
                </tr>
              ))}
              <tr className="border-b border-gray-800/50">
                <td className="py-3 px-4 text-gray-300">Price</td>
                <td className="py-3 px-4 text-center text-white font-medium">
                  $5–18/mo
                </td>
                <td className="py-3 px-4 text-center text-gray-400">
                  $8–10/mo
                </td>
                <td className="py-3 px-4 text-center text-gray-400">
                  $15/mo
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>
  )
}
