import { apiClient } from './client'

/**
 * Billing API — creates Stripe-hosted Checkout / Portal sessions and returns
 * their URLs for a full-page redirect. No Stripe.js on our origin (SAQ-A): the
 * card is only ever entered on checkout.stripe.com. The client sends a tier/pack
 * NAME — never a price id or amount (the server maps + binds the customer).
 */

// Subscription checkout for a tier. Returns the Checkout URL.
export async function createCheckout(tier) {
  const { data } = await apiClient.post('/billing/checkout', { tier })
  return data.url
}

// One-time credit-pack checkout (small|medium|large). Returns the Checkout URL.
export async function createPackCheckout(pack) {
  const { data } = await apiClient.post('/billing/checkout', { pack })
  return data.url
}

// Customer Portal session (manage/cancel/update card). Returns the portal URL.
export async function createPortal() {
  const { data } = await apiClient.post('/billing/portal')
  return data.url
}

// Full-page redirect to a Stripe-hosted URL. Isolated so tests can stub it.
export function redirectTo(url) {
  window.location.href = url
}
