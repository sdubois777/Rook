import { useEffect } from 'react'

/**
 * ?ref= capture.
 *
 * A referral link lands on the marketing page, but the code is not needed until
 * checkout — which is after Clerk's hosted sign-up. Sign-up is Clerk's drop-in
 * component and makes no API call we control, so there is nowhere on the server
 * to park the code across that round trip. localStorage is the only store that
 * survives it.
 *
 * The code is a hint for the checkout input, never an entitlement: the server
 * re-resolves it at /billing/checkout and is the only thing that decides what
 * discount, if any, applies.
 */

// Same naming style as LeagueContext's 'selectedLeague' / 'leagueFormatOverride'.
const STORAGE_KEY = 'referralCode'

// A sanity bound only. The real format (prefix, alphabet, length) lives in
// REFERRAL_PROGRAM on the server and the server judges the code — validating the
// shape here would be a second definition of it, free to drift.
const MAX_CODE_LENGTH = 64

function normalize(raw) {
  const code = (raw || '').trim().toUpperCase()
  if (!code || code.length > MAX_CODE_LENGTH) return ''
  return code
}

/** The captured code, or '' when there is none. */
export function readStoredReferralCode() {
  try {
    return normalize(localStorage.getItem(STORAGE_KEY))
  } catch {
    // localStorage unavailable (private mode) — the user can still type the code.
    return ''
  }
}

export function clearStoredReferralCode() {
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing to do — see readStoredReferralCode.
  }
}

/**
 * Read ?ref= out of a query string and store it. Returns the stored code.
 *
 * A fresh ?ref= REPLACES whatever was stored: it is the more recent statement of
 * who sent this visitor. A visit with no ?ref= leaves the stored code alone, so
 * ordinary navigation between the landing page and checkout does not lose it.
 *
 * Exported separately from the hook so it can be called with an explicit query
 * string, which is also how it is tested.
 */
export function captureReferralCode(search) {
  let code
  try {
    code = normalize(new URLSearchParams(search || '').get('ref'))
  } catch {
    // A query string URLSearchParams cannot parse carries no code we can trust.
    code = ''
  }
  if (!code) return readStoredReferralCode()
  try {
    localStorage.setItem(STORAGE_KEY, code)
  } catch {
    // localStorage unavailable — the code is lost at sign-up, which costs the
    // discount but breaks nothing.
  }
  return code
}

/** Capture once at boot, before any route decides what to render. */
export function useReferralCapture() {
  useEffect(() => {
    captureReferralCode(window.location.search)
  }, [])
}
