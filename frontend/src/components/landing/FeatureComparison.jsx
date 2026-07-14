import { Brain, History, Users, Radio } from 'lucide-react'

/**
 * "What consensus tools can't do" — makes the differentiation point WITHOUT
 * naming any competitor (a named competitor mostly teaches visitors it exists).
 * These are structural capabilities, not a feature-checkbox race: a tool built
 * on aggregated rankings cannot reason about cause, cannot know YOUR league, and
 * cannot sit in your draft.
 */
const POINTS = [
  {
    Icon: Brain,
    title: 'Reasons about why, not just where',
    body: 'Consensus rankings tell you a player is 14th. Rook reasons through the cause — a role change, a scheme shift, an injury upstream, a soft schedule — to find who the market is mispricing, and explains the why in plain English.',
  },
  {
    Icon: History,
    title: 'Knows your league, not the average one',
    body: 'It reads your actual league history and scoring: who your managers systematically overpay for, which positions they punt, what a win is worth in your format. Generic rankings are the same for everyone; your edge is not.',
  },
  {
    Icon: Users,
    title: 'Models your opponents',
    body: 'Rook tracks each opponent\'s tendencies and roster needs, so a trade or nomination is judged by how the people in your league will actually react — not by a league-agnostic average.',
  },
  {
    Icon: Radio,
    title: 'Sits in your draft with you',
    body: 'A live draft agent reads the room as it happens and gives real-time recommendations — a bid ceiling and the reasoning behind it — while the pick is still on the clock.',
  },
]

export default function FeatureComparison() {
  return (
    <section className="py-20 px-4 sm:px-6">
      <div className="max-w-4xl mx-auto">
        <h2 className="text-3xl sm:text-4xl font-bold text-white text-center mb-4">
          What consensus tools can&apos;t do
        </h2>
        <p className="text-gray-400 text-center mb-12 max-w-xl mx-auto">
          Every tool shows you the same rankings. Four things a tool built on
          aggregated consensus structurally cannot give you:
        </p>

        <div className="grid gap-5 sm:grid-cols-2">
          {POINTS.map((p) => (
            <div
              key={p.title}
              className="rounded-xl border border-gray-800 bg-gray-900/40 p-6"
            >
              <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-lg border border-brand-accent/20 bg-brand/10 text-brand-accent">
                <p.Icon size={22} strokeWidth={1.75} />
              </div>
              <h3 className="mb-2 text-lg font-semibold text-white">{p.title}</h3>
              <p className="text-sm leading-relaxed text-gray-400">{p.body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
