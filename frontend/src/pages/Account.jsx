import { useState } from 'react'
import { useUser, useClerk } from '@clerk/clerk-react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { apiClient } from '../api/client'

async function fetchAccountData() {
  const [me, credits, leagues] = await Promise.all([
    apiClient.get('/account/me'),
    apiClient.get('/account/credits'),
    apiClient.get('/account/leagues'),
  ])
  return {
    user: me.data,
    credits: credits.data,
    leagues: leagues.data,
  }
}

const TIER_LABELS = {
  intro: 'Intro \u2014 $5/mo',
  standard: 'Standard \u2014 $9/mo',
  pro: 'Pro \u2014 $18/mo',
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

const SCORING_LABELS = { ppr: 'PPR', half_ppr: 'Half PPR', standard: 'Standard' }

function LeagueCard({ league }) {
  const queryClient = useQueryClient()
  const [syncing, setSyncing] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState('')

  const handleSync = async () => {
    setError('')
    setSyncing(true)
    try {
      await apiClient.post(`/leagues/${league.id}/sync`)
      queryClient.invalidateQueries({ queryKey: ['account'] })
    } catch (err) {
      setError(err.response?.data?.message || 'Sync failed')
    } finally {
      setSyncing(false)
    }
  }

  const handleRemove = async () => {
    if (!window.confirm(`Remove "${league.league_name || league.league_id}"? This cannot be undone.`)) return
    setError('')
    setRemoving(true)
    try {
      await apiClient.delete(`/leagues/${league.id}`)
      // Remove from cache immediately (soft delete still returns from API)
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
      <div className="flex items-center justify-between">
        <div>
          <div className="font-medium flex items-center gap-2">
            {league.league_name || league.league_id}
            {isFinished && (
              <span className="text-xs text-gray-500 bg-gray-700 px-2 py-0.5 rounded">
                Finished
              </span>
            )}
          </div>
          <div className="text-sm text-gray-400">
            {league.platform} &middot; {league.team_count}-team &middot;{' '}
            {scoringLabel} &middot; {league.season_year}
          </div>
        </div>
        <div className="flex items-center gap-3">
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
    </div>
  )
}

export default function AccountPage() {
  const { user: clerkUser, isLoaded } = useUser()
  console.log('AccountPage rendering')
  console.log('Clerk user loaded:', isLoaded, clerkUser?.id)

  const { data, isLoading, error } = useQuery({
    queryKey: ['account'],
    queryFn: fetchAccountData,
    enabled: isLoaded && !!clerkUser,
  })

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

  const { user, credits, leagues } = data

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <div className="max-w-3xl mx-auto px-6 py-12">

        {/* Header */}
        <div className="mb-10">
          <h1 className="text-3xl font-bold mb-1">My Account</h1>
          <p className="text-gray-400">
            {clerkUser?.primaryEmailAddress?.emailAddress}
          </p>
        </div>

        {/* Plan */}
        <section className="bg-gray-900 rounded-xl border border-gray-800 p-6 mb-6">
          <div className="flex items-center justify-between mb-4">
            <div>
              <div className="text-sm text-gray-400 mb-1">Current Plan</div>
              <div className={`text-xl font-semibold ${TIER_COLORS[user.tier]}`}>
                {TIER_LABELS[user.tier] || user.tier}
              </div>
            </div>
            {user.tier !== 'pro' && (
              <button className="bg-blue-600 hover:bg-blue-500 text-white text-sm px-4 py-2 rounded-lg transition-colors">
                Upgrade
              </button>
            )}
          </div>
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
