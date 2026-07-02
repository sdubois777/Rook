import { useState } from 'react'
import { createPackCheckout, redirectTo } from '../../api/billing'

// Mirrors CREDIT_PACKS in backend/models/user.py (server is the source of truth;
// these are display-only — the server maps pack -> price and grants the credits).
const PACKS = [
  { id: 'small', usd: 5, credits: 75 },
  { id: 'medium', usd: 10, credits: 175 },
  { id: 'large', usd: 25, credits: 500 },
]

/**
 * One-time credit-pack purchase. Each option redirects to Stripe Checkout
 * (mode=payment) — no card fields on our origin (§0.A). Credits are granted by
 * the webhook on return; the parent polls /account/me for the new balance.
 */
export default function BuyCreditsCard() {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState('')

  const buy = async (pack) => {
    setBusy(pack)
    setError('')
    try {
      redirectTo(await createPackCheckout(pack))
    } catch {
      setError('Could not start checkout. Please try again.')
      setBusy(null)
    }
  }

  return (
    <div className="mt-6 border-t border-gray-800 pt-4">
      <div className="text-sm text-gray-400 mb-3">Buy credits</div>
      <div className="grid grid-cols-3 gap-3">
        {PACKS.map((p) => (
          <button
            key={p.id}
            onClick={() => buy(p.id)}
            disabled={busy !== null}
            className="rounded-lg border border-gray-700 hover:border-brand bg-gray-800 px-3 py-3 text-center disabled:opacity-50 transition-colors"
          >
            <div className="text-white font-semibold">{p.credits} cr</div>
            <div className="text-xs text-gray-400">${p.usd}</div>
          </button>
        ))}
      </div>
      {error && <p className="text-sm text-red-400 mt-2">{error}</p>}
    </div>
  )
}
