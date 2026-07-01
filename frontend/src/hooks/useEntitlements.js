import { useState, useEffect } from 'react'
import { apiClient } from '../api/client'

/**
 * Fail-open entitlement read for UX affordances ONLY — the backend gate is the
 * security boundary. Returns tierLimits=null until loaded (and on any error), so
 * callers default to the normal, UNLOCKED control rather than flashing a false
 * lock. A client-side tier check is never enforcement; it only avoids showing a
 * dead/broken control to a user whose tier lacks the feature.
 */
export function useEntitlements() {
  const [state, setState] = useState({
    tier: null,
    tierLimits: null,
    subscriptionStatus: null,
  })

  useEffect(() => {
    let alive = true
    apiClient
      .get('/account/me')
      .then(({ data }) => {
        if (!alive) return
        setState({
          tier: data.tier ?? null,
          tierLimits: data.tier_limits ?? null,
          subscriptionStatus: data.subscription_status ?? null,
        })
      })
      .catch(() => {
        /* fail open — leave tierLimits null so callers stay unlocked */
      })
    return () => {
      alive = false
    }
  }, [])

  return state
}

/** True only when we KNOW the tier lacks the feature (never on unknown). */
export function isFeatureLocked(tierLimits, feature) {
  return tierLimits ? tierLimits[feature] === false : false
}
