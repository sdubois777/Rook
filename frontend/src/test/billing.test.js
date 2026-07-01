import { describe, it, expect, beforeEach, vi } from 'vitest'

vi.mock('../api/client', () => ({ apiClient: { post: vi.fn() } }))

import { apiClient } from '../api/client'
import { createCheckout, createPackCheckout, createPortal } from '../api/billing'

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

  it('createPackCheckout posts a pack NAME and returns the url', async () => {
    apiClient.post.mockResolvedValue({ data: { url: 'https://checkout.stripe.com/pack' } })
    const url = await createPackCheckout('small')
    expect(apiClient.post).toHaveBeenCalledWith('/billing/checkout', { pack: 'small' })
    expect(url).toBe('https://checkout.stripe.com/pack')
  })

  it('createPortal posts to the portal endpoint and returns the url', async () => {
    apiClient.post.mockResolvedValue({ data: { url: 'https://billing.stripe.com/p' } })
    const url = await createPortal()
    expect(apiClient.post).toHaveBeenCalledWith('/billing/portal')
    expect(url).toBe('https://billing.stripe.com/p')
  })
})
