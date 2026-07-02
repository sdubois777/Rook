import { describe, it, expect, beforeEach } from 'vitest'
import api, { registerTokenGetter } from '../api/client'

// The shared API client turns billing gate errors into window events that drive
// UX affordances (the backend gate is the real boundary).
describe('api client billing gate events', () => {
  beforeEach(() => {
    registerTokenGetter(async () => 'tok')
  })

  function captureEvent(name, fn) {
    const events = []
    const handler = (e) => events.push(e)
    window.addEventListener(name, handler)
    return fn().finally(() => window.removeEventListener(name, handler)).then(() => events)
  }

  it('dispatches billing:feature-required on 403 feature_not_available', async () => {
    const original = api.defaults.adapter
    api.defaults.adapter = async (config) =>
      Promise.reject(Object.assign(new Error('forbidden'), {
        config,
        response: { status: 403, data: { error: 'feature_not_available', required_tier: 'standard' } },
      }))

    const events = await captureEvent('billing:feature-required', async () => {
      await expect(api.get('/whatever')).rejects.toBeTruthy()
    })
    api.defaults.adapter = original

    expect(events).toHaveLength(1)
    expect(events[0].detail.required_tier).toBe('standard')
  })

  it('dispatches billing:insufficient-credits on 402', async () => {
    const original = api.defaults.adapter
    api.defaults.adapter = async (config) =>
      Promise.reject(Object.assign(new Error('payment required'), {
        config,
        response: { status: 402, data: { error: 'insufficient_credits', required: 10, available: 3 } },
      }))

    const events = await captureEvent('billing:insufficient-credits', async () => {
      await expect(api.get('/whatever')).rejects.toBeTruthy()
    })
    api.defaults.adapter = original

    expect(events).toHaveLength(1)
    expect(events[0].detail.required).toBe(10)
    expect(events[0].detail.available).toBe(3)
  })

  it('dispatches billing:league-suspended on 403 league_suspended', async () => {
    const original = api.defaults.adapter
    api.defaults.adapter = async (config) =>
      Promise.reject(Object.assign(new Error('forbidden'), {
        config,
        response: { status: 403, data: { error: 'league_suspended', message: 'parked' } },
      }))

    const events = await captureEvent('billing:league-suspended', async () => {
      await expect(api.get('/whatever')).rejects.toBeTruthy()
    })
    api.defaults.adapter = original

    expect(events).toHaveLength(1)
    expect(events[0].detail.message).toBe('parked')
  })

  it('does NOT dispatch a billing event for an unrelated 403', async () => {
    const original = api.defaults.adapter
    api.defaults.adapter = async (config) =>
      Promise.reject(Object.assign(new Error('forbidden'), {
        config,
        response: { status: 403, data: { error: 'not_found' } },
      }))

    const events = await captureEvent('billing:feature-required', async () => {
      await expect(api.get('/whatever')).rejects.toBeTruthy()
    })
    api.defaults.adapter = original

    expect(events).toHaveLength(0)
  })
})
