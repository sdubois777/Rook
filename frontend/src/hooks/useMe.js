import { useQuery } from '@tanstack/react-query'
import { apiClient } from '../api/client'

/**
 * Shared read of the current user's entitlement + credit state (/account/me),
 * cached under ['me'] so the sidebar, trade page, etc. share one fetch. Credit-
 * spending actions should invalidate ['me'] on success so the balance updates.
 *
 * Returns the raw /account/me payload plus convenience fields. Display/affordance
 * only — the backend gate is the security boundary.
 */
export function useMe() {
  const query = useQuery({
    queryKey: ['me'],
    queryFn: () => apiClient.get('/account/me').then((r) => r.data),
    staleTime: 30_000,
  })
  const me = query.data
  return {
    ...query,
    tier: me?.tier ?? null,
    tierLimits: me?.tier_limits ?? null,
    credits: me?.credits_remaining ?? null,
    subscriptionStatus: me?.subscription_status ?? null,
  }
}
