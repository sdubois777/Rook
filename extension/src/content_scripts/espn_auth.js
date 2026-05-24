import browser from '../utils/browser.js'
import { triggerPassiveSync } from '../utils/passive_sync.js'

// Signal extension presence to React app
window.__draftmind__ = true

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', run)
} else {
  run()
}

async function run() {
  extractAndSendCookies()
  await triggerPassiveSync('espn')
}

async function extractAndSendCookies() {
  const cookies = document.cookie.split(';').reduce((acc, c) => {
    const [k, v] = c.trim().split('=')
    if (k) acc[k.trim()] = v || ''
    return acc
  }, {})

  const espn_s2 = cookies['espn_s2']
  const swid = cookies['SWID']

  if (!espn_s2 || !swid) return

  const leagueMatch = window.location.href.match(
    /leagueId=(\d+)|\/football\/.*?\/(\d+)/
  )
  const league_id = leagueMatch?.[1] || leagueMatch?.[2] || null

  browser.runtime.sendMessage({
    type: 'ESPN_COOKIES',
    payload: { espn_s2, swid, league_id },
  })

  await browser.storage.local.set({
    espn_connected: true,
    espn_league_id: league_id,
  })
}
