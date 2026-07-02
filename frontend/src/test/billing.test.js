import { describe, it, expect, beforeEach, vi } from 'vitest'

vi.mock('../api/client', () => ({ apiClient: { post: vi.fn() } }))

import { apiClient } from '../api/client'
import {
  createCheckout,
  createPackCheckout,
  createPortal,
  previewChangePlan,
  confirmChangePlan,
} from '../api/billing'

describe('billing api module', () => {
  beforeEach(() => {
    apiClient.post.mockReset()
  })

  it('createCheckout posts a tier NAME (no price) and returns the url', async () => {
    apiClient.post.mockResolvedValue({ data: { url: 'https://checkout.stripe.com/x' } })
    const url = await createCheckout('standard')
    expect(apiClient.post).toHaveBeenCalledWith('/billing/checkout', { tier: 'standard' })
    expect(url).toBe('https://checkout.stripe.com/x')
  })

  it('createPackCheckout posts a pack NAME to checkout-pack and returns the url', async () => {
    apiClient.post.mockResolvedValue({ data: { url: 'https://checkout.stripe.com/pack' } })
    const url = await createPackCheckout('small')
    expect(apiClient.post).toHaveBeenCalledWith('/billing/checkout-pack', { pack: 'small' })
    expect(url).toBe('https://checkout.stripe.com/pack')
  })

  it('createPortal posts to the portal endpoint and returns the url', async () => {
    apiClient.post.mockResolvedValue({ data: { url: 'https://billing.stripe.com/p' } })
    const url = await createPortal()
    expect(apiClient.post).toHaveBeenCalledWith('/billing/portal')
    expect(url).toBe('https://billing.stripe.com/p')
  })

  it('previewChangePlan posts a target tier NAME only', async () => {
    apiClient.post.mockResolvedValue({ data: { direction: 'upgrade', amount_due_today: 900 } })
    const data = await previewChangePlan('pro')
    expect(apiClient.post).toHaveBeenCalledWith('/billing/change-plan/preview', { target_tier: 'pro' })
    expect(data.direction).toBe('upgrade')
  })

  it('confirmChangePlan passes back the preview proration_date', async () => {
    apiClient.post.mockResolvedValue({ data: { status: 'applied' } })
    await confirmChangePlan('pro', 12345)
    expect(apiClient.post).toHaveBeenCalledWith('/billing/change-plan/confirm', {
      target_tier: 'pro',
      proration_date: 12345,
    })
  })
})
