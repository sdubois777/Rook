import browser from '../utils/browser.js'
import { MESSAGE_TYPES, STORAGE_KEYS } from '../utils/constants.js'
import { triggerPassiveSync } from '../utils/passive_sync.js'

// Signal extension presence to React app
window.__rook__ = true

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', run)
} else {
  run()
}

async function run() {
  await connectEspnLeague()
  await triggerPassiveSync('espn')
}

/**
 * Supply the league id from the URL and let the service worker read the ESPN
 * cookies (httpOnly-capable) and relay them. This script never touches cookie
 * values — the old document.cookie read couldn't see httpOnly espn_s2/SWID,
 * which is exactly what made the old capture unreliable.
 */
async function connectEspnLeague() {
  const leagueMatch = window.location.href.match(
    /leagueId=(\d+)|\/football\/.*?\/(\d+)/
  )
  const league_id = leagueMatch?.[1] || leagueMatch?.[2] || null
  if (!league_id) return

  let result
  try {
    result = await browser.runtime.sendMessage({
      type: MESSAGE_TYPES.ESPN_COOKIES,
      payload: { league_id },
    })
  } catch (err) {
    console.debug('Rook: ESPN connect relay failed', err)
    return
  }

  if (result?.ok) {
    await browser.storage.local.set({
      [STORAGE_KEYS.ESPN_CONNECTED]: true,
      [STORAGE_KEYS.ESPN_LEAGUE_ID]: league_id,
      [STORAGE_KEYS.ESPN_ERROR]: '',
    })
  } else {
    await browser.storage.local.set({
      [STORAGE_KEYS.ESPN_CONNECTED]: false,
      [STORAGE_KEYS.ESPN_ERROR]: result?.error || 'connect_failed',
    })
  }
}
