import { useEffect, useState } from 'react'
import { createCheckout, createPackCheckout, createPortal, redirectTo } from '../api/billing'
import { TIER_LABELS } from '../lib/constants'

/**
 * Global billing affordance. Listens for the `billing:*` window events the API
 * client dispatches on gate errors and shows a dismissible prompt with the right
 * CTA — an Upgrade for a wrong-tier feature, or Buy credits when out of credits.
 *
 * UX only: the backend gate is the security boundary. Nothing here grants access;
 * the CTA just routes the user to Stripe Checkout (the webhook flips the tier).
 */
export default function BillingNotice() {
  const [notice, setNotice] = useState(null) // { kind, ...detail }
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const onFeature = (e) => setNotice({ kind: 'feature', ...e.detail })
    const onCredits = (e) => setNotice({ kind: 'credits', ...e.detail })
    window.addEventListener('billing:feature-required', onFeature)
    window.addEventListener('billing:insufficient-credits', onCredits)
    return () => {
      window.removeEventListener('billing:feature-required', onFeature)
      window.removeEventListener('billing:insufficient-credits', onCredits)
    }
  }, [])

  if (!notice) return null

  const dismiss = () => {
    setNotice(null)
    setError('')
  }

  const go = async (fn) => {
    setBusy(true)
    setError('')
    try {
      redirectTo(await fn())
    } catch {
      setError('Could not start checkout. Please try again.')
      setBusy(false)
    }
  }

  const isFeature = notice.kind === 'feature'
  const requiredTier = notice.required_tier
  const title = isFeature ? 'Upgrade required' : 'Out of credits'
  const message = isFeature
    ? `This feature needs the ${TIER_LABELS[requiredTier] || requiredTier} plan.`
    : `You need ${notice.required} credits but have ${notice.available}.`

  return (
    <div className="fixed bottom-4 right-4 z-50 w-80 max-w-[calc(100vw-2rem)] rounded-xl border border-gray-700 bg-gray-900 shadow-xl p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-white">{title}</div>
          <p className="mt-1 text-sm text-gray-400">{message}</p>
        </div>
        <button
          onClick={dismiss}
          aria-label="Dismiss"
          className="text-gray-500 hover:text-gray-300 text-lg leading-none"
        >
          ×
        </button>
      </div>

      {error && <p className="mt-2 text-xs text-red-400">{error}</p>}

      <div className="mt-3 flex items-center gap-2">
        {isFeature ? (
          <button
            disabled={busy}
            onClick={() => go(() => createCheckout(requiredTier))}
            className="bg-brand hover:bg-brand-hover disabled:opacity-50 text-white text-sm font-semibold px-3 py-2 rounded-lg transition-colors"
          >
            {busy ? 'Starting…' : `Upgrade to ${requiredTier}`}
          </button>
        ) : (
          <button
            disabled={busy}
            onClick={() => go(() => createPackCheckout('small'))}
            className="bg-brand hover:bg-brand-hover disabled:opacity-50 text-white text-sm font-semibold px-3 py-2 rounded-lg transition-colors"
          >
            {busy ? 'Starting…' : 'Buy credits'}
          </button>
        )}
        {!isFeature && (
          <button
            disabled={busy}
            onClick={() => go(createPortal)}
            className="text-sm text-gray-400 hover:text-gray-200 px-2 py-2"
          >
            Manage plan
          </button>
        )}
      </div>
    </div>
  )
}
