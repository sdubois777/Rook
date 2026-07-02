import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/client', () => ({ apiClient: { get: vi.fn() } }))

import { apiClient } from '../api/client'
import { useMe } from '../hooks/useMe'

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('useMe', () => {
  beforeEach(() => apiClient.get.mockReset())

  it('exposes tier / credits / tierLimits from /account/me', async () => {
    apiClient.get.mockResolvedValue({
      data: {
        tier: 'standard',
        credits_remaining: 42,
        tier_limits: { trade_analyzer: true, trade_finder: false },
        subscription_status: 'active',
      },
    })
    const { result } = renderHook(() => useMe(), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.credits).toBe(42))
    expect(apiClient.get).toHaveBeenCalledWith('/account/me')
    expect(result.current.tier).toBe('standard')
    expect(result.current.tierLimits.trade_analyzer).toBe(true)
    expect(result.current.subscriptionStatus).toBe('active')
  })

  it('fails open to null fields before data loads', () => {
    apiClient.get.mockResolvedValue({ data: { tier: 'pro', credits_remaining: 9 } })
    const { result } = renderHook(() => useMe(), { wrapper: makeWrapper() })
    // Synchronous first render, before the query resolves — no false values.
    expect(result.current.credits).toBeNull()
    expect(result.current.tierLimits).toBeNull()
  })
})
