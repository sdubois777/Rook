import { apiClient } from './client'

/**
 * Billing API — creates Stripe-hosted Checkout / Portal sessions and returns
 * their URLs for a full-page redirect. No Stripe.js on our origin (SAQ-A): the
 * card is only ever entered on checkout.stripe.com. The client sends a tier/pack
 * NAME — never a price id or amount (the server maps + binds the customer).
 */

// Tier checkout. interval: 'monthly' (recurring sub) | 'season' (one-time
// season pass). Returns the Checkout URL.
export async function createCheckout(tier, interval = 'monthly') {
  const { data } = await apiClient.post('/billing/checkout', { tier, interval })
  return data.url
}

// One-time credit-pack checkout. Returns the Checkout URL.
export async function createPackCheckout(pack) {
  const { data } = await apiClient.post('/billing/checkout-pack', { pack })
  return data.url
}

// PUBLIC pricing sheet — tiers/prices/credit costs/packs, served straight from
// backend/models/user.py (the single source of truth). Never hardcode these.
export async function fetchPricing() {
  const { data } = await apiClient.get('/billing/pricing')
  return data
}

// Customer Portal session (manage/cancel/update card). Returns the portal URL.
export async function createPortal() {
  const { data } = await apiClient.post('/billing/portal')
  return data.url
}

/**
 * Preview a tier change. Charges/changes nothing. For an upgrade the server
 * returns amount_due_today + a proration_date to reuse on confirm; for a
 * downgrade it returns the period-end effective date. All amounts/dates come
 * from the server — never computed client-side.
 */
export async function previewChangePlan(targetTier) {
  const { data } = await apiClient.post('/billing/change-plan/preview', {
    target_tier: targetTier,
  })
  return data
}

// Apply a previewed change. Pass back the preview's proration_date (upgrades) so
// the charge matches the preview. Upgrade = applied now; downgrade = scheduled.
export async function confirmChangePlan(targetTier, prorationDate = null) {
  const { data } = await apiClient.post('/billing/change-plan/confirm', {
    target_tier: targetTier,
    proration_date: prorationDate,
  })
  return data
}

// Full-page redirect to a Stripe-hosted URL. Isolated so tests can stub it.
export function redirectTo(url) {
  window.location.href = url
}
