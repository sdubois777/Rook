/**
 * Shared TEST FIXTURE for usePricing — mirrors the SHAPE of GET /billing/pricing.
 * The values here are test data, not a pricing definition (the product's single
 * source of truth is backend/models/user.py; tests assert against this fixture).
 */
export const PRICING_FIXTURE = {
  tiers: [
    { id: 'free', label: 'Free', price_monthly_usd: 0, price_season_usd: 0, credits_signup_bonus: 30, max_leagues: 1, unlimited_features: false, live_draft: false, cross_league_view: false },
    { id: 'standard', label: 'Standard', price_monthly_usd: 8, price_season_usd: 29, credits_signup_bonus: 0, max_leagues: 1, unlimited_features: true, live_draft: true, cross_league_view: false },
    { id: 'pro', label: 'Pro', price_monthly_usd: 18, price_season_usd: 59, credits_signup_bonus: 0, max_leagues: null, unlimited_features: true, live_draft: true, cross_league_view: true },
  ],
  credit_costs: { trade_analysis: 1, waiver_wire: 2, trade_finder: 5 },
  packs: [{ id: 'credits_100', price_usd: 5, credits: 100 }],
}

export function pricingHookValue() {
  const byId = Object.fromEntries(PRICING_FIXTURE.tiers.map((t) => [t.id, t]))
  return {
    pricing: PRICING_FIXTURE,
    tiers: PRICING_FIXTURE.tiers,
    tierById: byId,
    packs: PRICING_FIXTURE.packs,
    tierLabel: (id) => {
      const t = byId[id]
      if (!t) return id
      return t.price_monthly_usd > 0 ? `${t.label} — $${t.price_monthly_usd}/mo` : t.label
    },
    creditCost: (a) => PRICING_FIXTURE.credit_costs[a] ?? null,
    isLoading: false,
    error: null,
  }
}
