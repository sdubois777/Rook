/**
 * ?ref= capture.
 *
 * The code arrives on a public URL and is not used until checkout, which is on
 * the far side of Clerk's hosted sign-up. Nothing on our server sees that
 * sign-up, so localStorage is the only thing that carries the code across it. If
 * this drops the code, the referrer is never credited and nobody finds out.
 */
import { render } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  captureReferralCode,
  clearStoredReferralCode,
  readStoredReferralCode,
  useReferralCapture,
} from '../hooks/useReferralCode'

function Probe() {
  useReferralCapture()
  return null
}

describe('referral code capture', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('stores the code from a ?ref= link', () => {
    captureReferralCode('?ref=ROOK-7K2M9X')
    expect(readStoredReferralCode()).toBe('ROOK-7K2M9X')
  })

  it('upper-cases and trims, so the stored code matches what the server expects', () => {
    captureReferralCode('?ref=%20rook-7k2m9x%20')
    expect(readStoredReferralCode()).toBe('ROOK-7K2M9X')
  })

  it('keeps the stored code when a later page has no ?ref=', () => {
    captureReferralCode('?ref=ROOK-7K2M9X')
    captureReferralCode('?billing=success')
    expect(readStoredReferralCode()).toBe('ROOK-7K2M9X')
  })

  it('a fresh ?ref= replaces the stored one', () => {
    captureReferralCode('?ref=ROOK-AAAAAA')
    captureReferralCode('?ref=ROOK-BBBBBB')
    expect(readStoredReferralCode()).toBe('ROOK-BBBBBB')
  })

  it('ignores an absurdly long value rather than storing it', () => {
    captureReferralCode(`?ref=${'A'.repeat(500)}`)
    expect(readStoredReferralCode()).toBe('')
  })

  it('returns empty and does not throw when localStorage is unavailable', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(() => captureReferralCode('?ref=ROOK-7K2M9X')).not.toThrow()
    expect(readStoredReferralCode()).toBe('')
  })

  it('clears on request', () => {
    captureReferralCode('?ref=ROOK-7K2M9X')
    clearStoredReferralCode()
    expect(readStoredReferralCode()).toBe('')
  })

  it('the boot hook captures from the real URL', () => {
    window.history.replaceState({}, '', '/?ref=ROOK-BOOT99')
    render(<Probe />)
    expect(readStoredReferralCode()).toBe('ROOK-BOOT99')
    window.history.replaceState({}, '', '/')
  })
})
