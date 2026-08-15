import api from './client'

/**
 * Referral API. Two calls, both authenticated.
 *
 * No percentage is computed here or anywhere else on the client — every rate in
 * these responses is served from REFERRAL_PROGRAM in backend/models/user.py, and
 * the UI renders what it is given.
 */

/**
 * The signed-in user's own referral state:
 * { code, share_url, referral_count, percent_off, percent_off_cap,
 *   percent_off_per_referral, eligible }.
 *
 * The code is minted on the first call, so this is a write on a new account.
 * It reports a count and a rate, never who redeemed.
 */
export async function fetchReferralState() {
  const { data } = await api.get('/account/referral')
  return data
}

/**
 * Check a discount code without applying anything.
 * Resolves to { valid, percent_off, message }.
 *
 * The interval matters: the server refuses a code on a season interval and says
 * so in `message`. Checkout re-resolves the code independently, so a pass here
 * is a preview, not a guarantee.
 */
export async function validateCode(code, interval = 'monthly') {
  const { data } = await api.post('/billing/validate-code', { code, interval })
  return data
}
