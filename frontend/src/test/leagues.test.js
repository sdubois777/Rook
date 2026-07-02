import { describe, it, expect, beforeEach, vi } from 'vitest'

vi.mock('../api/client', () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }))

import { apiClient } from '../api/client'
import { fetchLeagueLimitState, resolveLeagueLimit } from '../api/leagues'

describe('leagues api', () => {
  beforeEach(() => {
    apiClient.get.mockReset()
    apiClient.post.mockReset()
  })

  it('fetchLeagueLimitState GETs the limit-state', async () => {
    apiClient.get.mockResolvedValue({ data: { over_limit: true, active_count: 3 } })
    const data = await fetchLeagueLimitState()
    expect(apiClient.get).toHaveBeenCalledWith('/account/leagues/limit-state')
    expect(data.over_limit).toBe(true)
  })

  it('resolveLeagueLimit POSTs the keep ids', async () => {
    apiClient.post.mockResolvedValue({ data: { over_limit: false } })
    await resolveLeagueLimit(['a', 'b'])
    expect(apiClient.post).toHaveBeenCalledWith('/account/leagues/resolve-limit', {
      keep: ['a', 'b'],
    })
  })
})
