import browser from './browser.js'
import { getDraftToken, getApiBase } from './api.js'

const SYNC_DEBOUNCE_MS = 30 * 60 * 1000 // 30 minutes

export async function triggerPassiveSync(platform) {
  const token = await getDraftToken()
  if (!token) return

  const key = `last_sync_${platform}`
  const result = await browser.storage.local.get(key)
  const lastSync = result[key] || 0

  if (Date.now() - lastSync < SYNC_DEBOUNCE_MS) {
    console.debug(`DraftMind: sync debounced for ${platform}`)
    return
  }

  try {
    const resp = await fetch(
      `${getApiBase()}/leagues/sync-platform/${platform}`,
      {
        method: 'POST',
        headers: { 'X-Draft-Token': token },
      }
    )
    if (resp.ok) {
      await browser.storage.local.set({ [key]: Date.now() })
      console.debug(`DraftMind: passive sync ok for ${platform}`)
    }
  } catch (err) {
    // Always silent fail — never interrupt user
    console.debug(`DraftMind: passive sync failed for ${platform}`, err)
  }
}
