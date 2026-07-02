import { useState } from 'react'
import { previewChangePlan, confirmChangePlan } from '../../api/billing'
import { TIER_LABELS } from '../../lib/constants'

const TIER_ORDER = ['intro', 'standard', 'pro']

function formatUsd(cents) {
  return `$${(cents / 100).toFixed(2)}`
}

function formatDate(iso) {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
    })
  } catch {
    return iso
  }
}

/**
 * In-app tier change: preview (charges/shows nothing) then confirm. Upgrades are
 * immediate + prorated against the card on file; downgrades are scheduled for
 * period-end. All amounts/dates come from the server — never computed here. No
 * card fields (§0.A). onApplied() is called after an immediate upgrade so the
 * parent can poll /account/me for the flipped tier.
 */
export default function ChangePlanCard({ currentTier, onApplied }) {
  const [target, setTarget] = useState(null)
  const [preview, setPreview] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const reset = () => {
    setTarget(null)
    setPreview(null)
    setError('')
  }

  const startPreview = async (tier) => {
    setBusy(true)
    setError('')
    setTarget(tier)
    setResult(null)
    try {
      setPreview(await previewChangePlan(tier))
    } catch (e) {
      setError(e.response?.data?.detail || 'Could not preview this change.')
      setTarget(null)
    } finally {
      setBusy(false)
    }
  }

  const confirm = async () => {
    setBusy(true)
    setError('')
    try {
      const res = await confirmChangePlan(target, preview.proration_date ?? null)
      setResult(res)
      reset()
      if (res.status === 'applied') onApplied?.()
    } catch (e) {
      setError(e.response?.data?.detail || 'Could not apply this change.')
    } finally {
      setBusy(false)
    }
  }

  const options = TIER_ORDER.filter((t) => t !== currentTier)

  return (
    <div className="mt-4 border-t border-gray-800 pt-4">
      <div className="text-sm text-gray-400 mb-2">Change plan</div>

      {result && (
        <p className="text-sm text-green-400 mb-3">
          {result.status === 'applied'
            ? `Upgraded to ${TIER_LABELS[result.target_tier] || result.target_tier}.`
            : `Scheduled: ${TIER_LABELS[result.target_tier] || result.target_tier} on ${formatDate(result.effective)}.`}
        </p>
      )}

      {!preview && (
        <div className="flex flex-wrap gap-2">
          {options.map((t) => (
            <button
              key={t}
              onClick={() => startPreview(t)}
              disabled={busy}
              className="text-sm border border-gray-700 hover:border-brand text-gray-200 px-3 py-2 rounded-lg disabled:opacity-50 transition-colors"
            >
              Change to {TIER_LABELS[t] || t}
            </button>
          ))}
        </div>
      )}

      {preview && (
        <div className="rounded-lg border border-gray-700 bg-gray-800 p-4">
          {preview.direction === 'upgrade' ? (
            <p className="text-sm text-gray-200">
              You'll be charged{' '}
              <span className="font-semibold">{formatUsd(preview.amount_due_today)}</span>{' '}
              today and move to{' '}
              <span className="font-semibold">
                {TIER_LABELS[preview.target_tier] || preview.target_tier}
              </span>{' '}
              now.
            </p>
          ) : (
            <>
              <p className="text-sm text-gray-200">
                You'll keep {TIER_LABELS[currentTier] || currentTier} until{' '}
                <span className="font-semibold">{formatDate(preview.effective)}</span>, then
                move to{' '}
                <span className="font-semibold">
                  {TIER_LABELS[preview.target_tier] || preview.target_tier}
                </span>
                . No charge today.
              </p>
              {preview.max_active_leagues != null &&
                preview.active_leagues > preview.max_active_leagues && (
                  <p className="mt-2 text-sm text-yellow-500">
                    {TIER_LABELS[preview.target_tier] || preview.target_tier} allows{' '}
                    {preview.max_active_leagues} active leagues; you have{' '}
                    {preview.active_leagues}. You'll choose which stay active when this
                    takes effect on {formatDate(preview.effective)}.
                  </p>
                )}
            </>
          )}

          <div className="mt-3 flex items-center gap-2">
            <button
              onClick={confirm}
              disabled={busy}
              className="bg-brand hover:bg-brand-hover disabled:opacity-50 text-white text-sm font-semibold px-3 py-2 rounded-lg transition-colors"
            >
              {busy ? 'Working…' : 'Confirm'}
            </button>
            <button
              onClick={reset}
              disabled={busy}
              className="text-sm text-gray-400 hover:text-gray-200 px-2 py-2"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && <p className="text-sm text-red-400 mt-2">{error}</p>}
    </div>
  )
}
