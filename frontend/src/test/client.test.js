import { describe, it, expect, beforeEach } from 'vitest'
import api, { getTokenWithRetry, registerTokenGetter } from '../api/client'

describe('getTokenWithRetry', () => {
  it('returns a token immediately when available', async () => {
    const token = await getTokenWithRetry(5, 1, async () => 'tok')
    expect(token).toBe('tok')
  })

  it('returns a token after a delay (getter not ready at first)', async () => {
    let calls = 0
    const getter = async () => {
      calls += 1
      return calls >= 2 ? 'tok' : null
    }
    const token = await getTokenWithRetry(5, 1, getter)
    expect(token).toBe('tok')
    expect(calls).toBeGreaterThanOrEqual(2)
  })

  it('gives up and returns null after max retries', async () => {
    const token = await getTokenWithRetry(3, 1, async () => null)
    expect(token).toBeNull()
  })

  it('tolerates a getter that throws', async () => {
    let calls = 0
    const getter = async () => {
      calls += 1
      if (calls === 1) throw new Error('clerk not ready')
      return 'tok'
    }
    expect(await getTokenWithRetry(5, 1, getter)).toBe('tok')
  })
})

describe('api client 401 retry', () => {
  beforeEach(() => {
    registerTokenGetter(async () => 'tok')
  })

  it('retries a 401 response once with a fresh token, then succeeds', async () => {
    let calls = 0
    const original = api.defaults.adapter
    api.defaults.adapter = async (config) => {
      calls += 1
      if (calls === 1) {
        return Promise.reject(
          Object.assign(new Error('Unauthorized'), {
            config,
            response: { status: 401, data: {} },
          })
        )
      }
      return { data: { ok: true }, status: 200, statusText: 'OK', headers: {}, config }
    }
    try {
      const res = await api.get('/whatever')
      expect(res.data.ok).toBe(true)
      expect(calls).toBe(2) // original + one retry
    } finally {
      api.defaults.adapter = original
    }
  })
})
