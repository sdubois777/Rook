import { useState, useEffect } from 'react'
import { useUser, useClerk } from '@clerk/clerk-react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { apiClient } from '../api/client'
import { createPortal, redirectTo } from '../api/billing'
import { SCORING_LABELS, TIER_LABELS } from '../lib/constants'

const SUBSCRIPTION_STATUS_COPY = {
  past_due: 'Your last payment failed — update your payment method to keep your plan.',
  canceling: 'Your plan is set to cancel and will end at the end of the current billing period.',
}

// Manage subscription (Stripe Customer Portal) — cancel, update card, etc.
function ManageSubscriptionButton() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const open = async () => {
    setBusy(true)
    setError('')
    try {
      redirectTo(await createPortal())
    } catch {
      setError('Could not open the billing portal.')
      setBusy(false)
    }
  }
  return (
    <div className="text-right">
      <button
        onClick={open}
        disabled={busy}
        className="bg-brand hover:bg-brand-hover disabled:opacity-50 text-white text-sm px-4 py-2 rounded-lg transition-colors"
      >
        {busy ? 'Opening…' : 'Manage subscription'}
      </button>
      {error && <p className="text-red-400 text-xs mt-1">{error}</p>}
    </div>
  )
}

async function fetchAccountData() {
  const [me, credits, leagues, tokenResp] = await Promise.all([
    apiClient.get('/account/me'),
    apiClient.get('/account/credits'),
    apiClient.get('/account/leagues'),
    apiClient.get('/account/draft-token').catch(() => ({ data: {} })),
  ])
  return {
    user: me.data,
    credits: credits.data,
    leagues: leagues.data,
    draftToken: tokenResp.data.draft_token || null,
  }
}

const TIER_COLORS = {
  intro: 'text-gray-400',
  standard: 'text-blue-400',
  pro: 'text-purple-400',
}

function SignOutButton() {
  const { signOut } = useClerk()
  return (
    <button
      onClick={() => signOut()}
      className="text-sm text-gray-500 hover:text-gray-300 transition-colors"
    >
      Sign out
    </button>
  )
}

function DraftTokenSection({ token, onRevoke }) {
  const [copied, setCopied] = useState(false)
  const [revoking, setRevoking] = useState(false)

  const handleCopy = () => {
    navigator.clipboard.writeText(token)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleRevoke = async () => {
    if (!window.confirm('Revoke this token? Your browser extension will need a new token.')) return
    setRevoking(true)
    try {
      await onRevoke()
    } finally {
      setRevoking(false)
    }
  }

  return (
    <section className="bg-gray-900 rounded-xl border border-gray-800 p-6 mb-6">
      <h2 className="text-lg font-semibold mb-2">Browser Extension</h2>
      <p className="text-sm text-gray-400 mb-4">
        Paste this token into the Rook extension popup to connect.
      </p>
      <div className="flex items-center gap-2">
        <code className="flex-1 bg-gray-800 text-gray-300 text-sm px-3 py-2 rounded-lg font-mono truncate">
          {token}
        </code>
        <button
          onClick={handleCopy}
          className="text-sm text-blue-400 hover:text-blue-300 whitespace-nowrap transition-colors"
        >
          {copied ? 'Copied!' : 'Copy'}
        </button>
        <button
          onClick={handleRevoke}
          disabled={revoking}
          className="text-sm text-red-400 hover:text-red-300 disabled:opacity-50 whitespace-nowrap transition-colors"
        >
          {revoking ? 'Revoking...' : 'Revoke'}
        </button>
      </div>
    </section>
  )
}

function LeagueCard({ league }) {
  const queryClient = useQueryClient()
  const [syncing, setSyncing] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState('')
  const [syncWarnings, setSyncWarnings] = useState([])

  const handleSync = async () => {
    setError('')
    setSyncWarnings([])
    setSyncing(true)
    try {
      const resp = await apiClient.post(`/leagues/${league.id}/sync`)
      setSyncWarnings(resp.data.warnings || [])
      queryClient.invalidateQueries({ queryKey: ['account'] })
    } catch (err) {
      setError(err.response?.data?.message || 'Sync failed')
    } finally {
      setSyncing(false)
    }
  }

  const handleRemove = async () => {
    if (!window.confirm(
      `Remove "${league.league_name || league.league_id}"?\n\n` +
      `This will permanently delete all draft history and sync data for this league. ` +
      `You can re-import it later from ${league.platform}.\n\n` +
      `This cannot be undone.`
    )) return
    setError('')
    setRemoving(true)
    try {
      await apiClient.delete(`/leagues/${league.id}`)
      // Remove from cache immediately
      queryClient.setQueryData(['account'], (old) =>
        old ? { ...old, leagues: old.leagues.filter((l) => l.id !== league.id) } : old
      )
    } catch (err) {
      setError(err.response?.data?.message || 'Remove failed')
      setRemoving(false)
    }
  }

  const scoringLabel = SCORING_LABELS[league.scoring] || league.scoring?.toUpperCase() || '—'

  const isFinished = !league.is_active

  return (
    <div className={`bg-gray-800 rounded-lg p-4 ${isFinished ? 'opacity-75' : ''}`}>
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
        <div className="min-w-0">
          <div className="font-medium flex flex-wrap items-center gap-2">
            {league.league_name || league.league_id}
            {isFinished && (
              <span className="text-xs text-gray-500 bg-gray-700 px-2 py-0.5 rounded">
                {league.season_year} Season &middot; Finished
              </span>
            )}
          </div>
          <div className="text-sm text-gray-400">
            {league.platform} &middot; {league.team_count}-team &middot;{' '}
            {scoringLabel} &middot; {league.season_year}
          </div>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <div className="text-xs text-gray-500">
            {league.last_synced
              ? `Synced ${new Date(league.last_synced).toLocaleDateString()}`
              : 'Not synced'}
          </div>
          <button
            onClick={handleSync}
            disabled={syncing || removing}
            className="text-xs text-blue-400 hover:text-blue-300 disabled:opacity-50 transition-colors"
          >
            {syncing ? 'Syncing...' : 'Re-sync'}
          </button>
          <button
            onClick={handleRemove}
            disabled={syncing || removing}
            className="text-xs text-red-400 hover:text-red-300 disabled:opacity-50 transition-colors"
          >
            {removing ? 'Removing...' : 'Remove'}
          </button>
        </div>
      </div>
      {error && <p className="text-red-400 text-xs mt-2">{error}</p>}
      {syncWarnings.length > 0 && (
        <div className="text-xs text-yellow-500 mt-1">
          {syncWarnings.join(' · ')}
        </div>
      )}
    </div>
  )
}

export default function AccountPage() {
  const { user: clerkUser, isLoaded } = useUser()

  const { data, isLoading, error } = useQuery({
    queryKey: ['account'],
    queryFn: fetchAccountData,
    enabled: isLoaded && !!clerkUser,
  })

  const queryClient = useQueryClient()
  // Derived from the URL at mount so we don't setState synchronously in the effect.
  const [confirming, setConfirming] = useState(
    () => new URLSearchParams(window.location.search).get('billing') === 'success'
  )

  // Checkout success return: the URL grants NOTHING — the webhook is authoritative.
  // Poll /account/me a few times so the freshly-flipped tier appears without a
  // manual refresh, handling the webhook-vs-redirect race, then clean the URL.
  useEffect(() => {
    if (!confirming) return
    let tries = 0
    const iv = setInterval(() => {
      tries += 1
      queryClient.invalidateQueries({ queryKey: ['account'] })
      const latest = queryClient.getQueryData(['account'])
      if (tries >= 6 || latest?.user?.subscription_status) {
        clearInterval(iv)
        setConfirming(false)
        window.history.replaceState({}, '', '/account')
      }
    }, 1500)
    return () => clearInterval(iv)
  }, [confirming, queryClient])

  if (isLoading) {
    return (
      <div className="min-h-screen bg-gray-950 flex items-center justify-center">
        <div className="text-gray-400">Loading...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="min-h-screen bg-gray-950 flex items-center justify-center">
        <div className="text-red-400">Failed to load account data</div>
      </div>
    )
  }

  const { user, credits, leagues, draftToken } = data

  const handleRevokeToken = async () => {
    await apiClient.post('/account/draft-token/revoke')
    queryClient.invalidateQueries({ queryKey: ['account'] })
  }

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <div className="max-w-3xl mx-auto px-4 sm:px-6 py-12">

        {/* Header */}
        <div className="mb-10">
          <h1 className="text-3xl font-bold mb-1">My Account</h1>
          <p className="text-gray-400">
            {clerkUser?.primaryEmailAddress?.emailAddress}
          </p>
        </div>

        {/* Checkout-return confirmation — the webhook flips the tier, we just poll. */}
        {confirming && (
          <div className="bg-brand/10 border border-brand rounded-xl p-4 mb-6 text-sm text-blue-200">
            Confirming your upgrade…
          </div>
        )}

        {/* Plan */}
        <section className="bg-gray-900 rounded-xl border border-gray-800 p-6 mb-6">
          <div className="flex items-center justify-between mb-2">
            <div>
              <div className="text-sm text-gray-400 mb-1">Current Plan</div>
              <div className={`text-xl font-semibold ${TIER_COLORS[user.tier]}`}>
                {TIER_LABELS[user.tier] || user.tier}
              </div>
            </div>
            {user.subscription_status ? (
              <ManageSubscriptionButton />
            ) : user.tier !== 'pro' ? (
              <Link
                to="/pricing"
                className="bg-brand hover:bg-brand-hover text-white text-sm px-4 py-2 rounded-lg transition-colors"
              >
                Upgrade
              </Link>
            ) : null}
          </div>
          {SUBSCRIPTION_STATUS_COPY[user.subscription_status] && (
            <p
              className={`text-sm mt-2 ${
                user.subscription_status === 'past_due' ? 'text-red-400' : 'text-yellow-500'
              }`}
            >
              {SUBSCRIPTION_STATUS_COPY[user.subscription_status]}
            </p>
          )}
        </section>

        {/* Credits */}
        <section className="bg-gray-900 rounded-xl border border-gray-800 p-6 mb-6">
          <h2 className="text-lg font-semibold mb-4">Credits</h2>

          <div className="mb-4">
            <div className="flex justify-between text-sm mb-2">
              <span className="text-gray-400">Balance</span>
              <span className="text-white font-medium">{credits.balance} credits</span>
            </div>
            <div className="w-full bg-gray-800 rounded-full h-2">
              <div
                className="bg-blue-500 h-2 rounded-full transition-all"
                style={{
                  width: `${Math.min(
                    100,
                    (credits.balance / Math.max(credits.balance + (credits.usage_last_30_days || 0), 1)) * 100
                  )}%`,
                }}
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4 text-sm mb-6">
            <div>
              <div className="text-gray-400">Monthly allowance</div>
              <div className="text-white">
                {credits.monthly_allowance > 0
                  ? `+${credits.monthly_allowance}/mo`
                  : 'None (Intro plan)'}
              </div>
            </div>
            <div>
              <div className="text-gray-400">Used (30 days)</div>
              <div className="text-white">{credits.usage_last_30_days || 0} credits</div>
            </div>
          </div>

          {credits.history && credits.history.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-gray-400 mb-3">Recent Usage</h3>
              <div className="space-y-2">
                {credits.history.slice(0, 5).map((item, i) => (
                  <div key={i} className="flex justify-between text-sm">
                    <span className="text-gray-300 capitalize">
                      {item.action.replace(/_/g, ' ')}
                    </span>
                    <span className="text-red-400">-{item.credits_used} cr</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>

        {/* Browser Extension */}
        {draftToken && (
          <DraftTokenSection token={draftToken} onRevoke={handleRevokeToken} />
        )}

        {/* Leagues */}
        <section className="bg-gray-900 rounded-xl border border-gray-800 p-6 mb-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold">My Leagues</h2>
            <Link to="/league-setup" className="text-sm text-blue-400 hover:text-blue-300">
              + Add League
            </Link>
          </div>

          {leagues.length === 0 ? (
            <p className="text-gray-500 text-sm">
              No leagues connected yet. Add your first league to get started.
            </p>
          ) : (
            <div className="space-y-3">
              {leagues.map((league) => (
                <LeagueCard key={league.id} league={league} />
              ))}
            </div>
          )}
        </section>

        {/* Sign out */}
        <div className="text-center">
          <SignOutButton />
        </div>
      </div>
    </div>
  )
}
