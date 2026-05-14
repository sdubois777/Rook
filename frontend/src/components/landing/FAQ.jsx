import { useState } from 'react'

const ITEMS = [
  {
    q: 'How is this different from FantasyPros Premium?',
    a: 'FantasyPros aggregates expert consensus rankings. DraftMind uses AI agents that reason about cause-and-effect — role changes, scheme fits, injury risk, and schedule difficulty — to find players the consensus is mispricing. It\'s the difference between "experts say he\'s ranked 12th" and "here\'s why he\'s undervalued in YOUR league."',
  },
  {
    q: 'Does it work for snake drafts?',
    a: 'Yes. DraftMind supports both auction and snake draft formats. The valuation engine adapts pick values and recommendations to your draft type.',
  },
  {
    q: 'What leagues and platforms are supported?',
    a: 'Yahoo Fantasy, ESPN, and Sleeper. Import your league in one click — the system pulls your league history, opponent tendencies, and scoring settings automatically.',
  },
  {
    q: 'How accurate are the projections?',
    a: 'In 2025 season backtesting, DraftMind achieved 74.1% signal accuracy, 93% buy signal accuracy, and a 0.88 correlation between projected and actual PPR points. Real results, not hand-picked examples.',
  },
  {
    q: 'Is my league data private?',
    a: 'Absolutely. Your league data is stored securely and never shared with other users. We don\'t sell data, and your analysis is visible only to you.',
  },
  {
    q: 'What happens after my credits run out?',
    a: 'You can still browse projections, the draft board, news, and injury monitoring for free. Credits are only consumed by trade analysis, waiver wire, and trade finder features. You can buy credit packs anytime or wait for your monthly refill.',
  },
  {
    q: 'Can I cancel anytime?',
    a: 'Yes. Cancel anytime from your account page. No contracts, no cancellation fees. Your credits remain available until they\'re used.',
  },
]

export default function FAQ() {
  const [openIndex, setOpenIndex] = useState(null)

  return (
    <section className="py-20 px-4 sm:px-6">
      <div className="max-w-3xl mx-auto">
        <h2 className="text-3xl sm:text-4xl font-bold text-white text-center mb-12">
          Frequently Asked Questions
        </h2>

        <div className="space-y-3">
          {ITEMS.map((item, i) => (
            <div
              key={i}
              className="border border-gray-800 rounded-xl overflow-hidden"
            >
              <button
                onClick={() => setOpenIndex(openIndex === i ? null : i)}
                className="w-full flex items-center justify-between px-6 py-4 text-left hover:bg-gray-900/40 transition-colors"
              >
                <span className="text-sm font-medium text-gray-200 pr-4">
                  {item.q}
                </span>
                <svg
                  className={`w-5 h-5 text-gray-500 shrink-0 transition-transform ${
                    openIndex === i ? 'rotate-180' : ''
                  }`}
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              {openIndex === i && (
                <div className="px-6 pb-5 text-sm text-gray-400 leading-relaxed">
                  {item.a}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
