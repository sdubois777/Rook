/**
 * usePricing — the ONLY way the frontend learns prices, credit costs, grants,
 * and pack sizes. Fetches the public /billing/pricing sheet (served straight
 * from backend/models/user.py, the single source of truth). Hardcoding any of
 * these numbers client-side re-creates the four-way pricing drift this hook
 * exists to kill.
 */
import { useQuery } from '@tanstack/react-query'
import { fetchPricing } from '../api/billing'

export function usePricing() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['pricing'],
    queryFn: fetchPricing,
    staleTime: 60 * 60 * 1000, // pricing changes at deploy cadence, not runtime
    retry: 1,
  })

  const tiers = data?.tiers ?? []
  const byId = Object.fromEntries(tiers.map((t) => [t.id, t]))

  /** "Standard — $8/mo" (free tiers label without a price). */
  function tierLabel(tierId) {
    const t = byId[tierId]
    if (!t) return tierId
    return t.price_monthly_usd > 0
      ? `${t.label} — $${t.price_monthly_usd}/mo`
      : t.label
  }

  /** Credit cost for a metered action, or null while loading. */
  function creditCost(action) {
    return data?.credit_costs?.[action] ?? null
  }

  return {
    pricing: data,
    tiers,
    tierById: byId,
    packs: data?.packs ?? [],
    tierLabel,
    creditCost,
    isLoading,
    error,
  }
}
