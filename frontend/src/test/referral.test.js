import { describe, it, expect, beforeEach, vi } from 'vitest'

vi.mock('../api/client', () => ({ default: { get: vi.fn(), post: vi.fn() } }))

import api from '../api/client'
import { fetchReferralState, validateCode } from '../api/referral'

describe('referral api module', () => {
  beforeEach(() => {
    api.get.mockReset()
    api.post.mockReset()
  })

  it('fetchReferralState reads the caller-scoped endpoint (no user id in the path)', async () => {
    const state = {
      code: 'ROOK-7K2M9X',
      share_url: 'https://rookff.com/?ref=ROOK-7K2M9X',
      referral_count: 2,
      percent_off: 20,
      percent_off_cap: 50,
      percent_off_per_referral: 10,
      eligible: true,
    }
    api.get.mockResolvedValue({ data: state })

    const data = await fetchReferralState()

    expect(api.get).toHaveBeenCalledWith('/account/referral')
    expect(data).toEqual(state)
  })

  it('validateCode posts the code and the interval', async () => {
    api.post.mockResolvedValue({
      data: { valid: true, percent_off: 30, message: '30% off your first month.' },
    })

    const data = await validateCode('ROOK-7K2M9X')

    expect(api.post).toHaveBeenCalledWith('/billing/validate-code', {
      code: 'ROOK-7K2M9X',
      interval: 'monthly',
    })
    expect(data.percent_off).toBe(30)
  })

  it('validateCode passes a season interval through so the server can refuse it', async () => {
    api.post.mockResolvedValue({
      data: { valid: false, percent_off: 0, message: 'Monthly plans only.' },
    })

    const data = await validateCode('ROOK-7K2M9X', 'season')

    expect(api.post).toHaveBeenCalledWith('/billing/validate-code', {
      code: 'ROOK-7K2M9X',
      interval: 'season',
    })
    expect(data.valid).toBe(false)
  })
})
