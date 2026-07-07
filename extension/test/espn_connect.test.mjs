import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const swPath = join(__dirname, '..', 'src', 'background', 'service_worker.js')
const authPath = join(__dirname, '..', 'src', 'content_scripts', 'espn_auth.js')

// ── Behavioral: mock the extension globals BEFORE importing the module. The
// browser shim binds globalThis.chrome at import, so we install one persistent
// chrome object whose behavior reads test-scoped state we mutate per test. ──
let _draftToken = null
let _cookies = {}
let _fetch = null
const _cookieNamesRequested = []

globalThis.chrome = {
  runtime: { onMessage: { addListener() {} } },
  storage: {
    local: {
      async get(keys) {
        const arr = Array.isArray(keys) ? keys : [keys]
        const out = {}
        for (const k of arr) if (k === 'draft_token' && _draftToken) out.draft_token = _draftToken
        return out
      },
      async set() {},
    },
  },
  cookies: {
    async get({ url, name }) {
      assert.equal(url, 'https://fantasy.espn.com', 'cookie read must be scoped to the ESPN origin')
      _cookieNamesRequested.push(name)
      return name in _cookies ? { value: _cookies[name] } : null
    },
  },
}
globalThis.fetch = (...args) => _fetch(...args)

const { connectEspnFromCookies } = await import('../src/background/service_worker.js')

function reset() {
  _draftToken = 'tok_123'
  _cookies = { espn_s2: 'S2VALUE', SWID: '{SWID-VALUE}' }
  _cookieNamesRequested.length = 0
  _fetch = null
}

test('connectEspnFromCookies posts espn_s2/SWID to the extension endpoint with X-Draft-Token', async () => {
  reset()
  let captured = null
  _fetch = async (url, opts) => {
    captured = { url, opts }
    return { ok: true, status: 200, async json() { return { status: 'connected', league_id: 'L1' } } }
  }
  const result = await connectEspnFromCookies({ league_id: '77' })

  assert.equal(result.ok, true)
  assert.match(captured.url, /\/leagues\/connect\/espn\/extension$/)
  assert.equal(captured.opts.method, 'POST')
  assert.equal(captured.opts.headers['X-Draft-Token'], 'tok_123')
  const body = JSON.parse(captured.opts.body)
  assert.equal(body.league_id, '77')
  assert.equal(body.espn_s2, 'S2VALUE')
  assert.equal(body.swid, '{SWID-VALUE}')
})

test('reads only espn_s2 and SWID by name — no other espn.com cookies', async () => {
  reset()
  _fetch = async () => ({ ok: true, status: 200, async json() { return {} } })
  await connectEspnFromCookies({ league_id: '77' })
  assert.deepEqual([..._cookieNamesRequested].sort(), ['SWID', 'espn_s2'])
})

test('no draft token → no_draft_token, does not call fetch', async () => {
  reset()
  _draftToken = null
  let called = false
  _fetch = async () => { called = true; return { ok: true, status: 200, async json() { return {} } } }
  const result = await connectEspnFromCookies({ league_id: '77' })
  assert.deepEqual(result, { ok: false, error: 'no_draft_token' })
  assert.equal(called, false)
})

test('missing ESPN cookies → no_espn_cookies, does not call fetch', async () => {
  reset()
  _cookies = {} // logged out of ESPN
  let called = false
  _fetch = async () => { called = true; return { ok: true, status: 200, async json() { return {} } } }
  const result = await connectEspnFromCookies({ league_id: '77' })
  assert.deepEqual(result, { ok: false, error: 'no_espn_cookies' })
  assert.equal(called, false)
})

test('backend 401 → invalid_draft_token', async () => {
  reset()
  _fetch = async () => ({ ok: false, status: 401, async json() { return {} } })
  const result = await connectEspnFromCookies({ league_id: '77' })
  assert.deepEqual(result, { ok: false, error: 'invalid_draft_token' })
})

test('backend 422 → invalid_espn_cookies', async () => {
  reset()
  _fetch = async () => ({ ok: false, status: 422, async json() { return {} } })
  const result = await connectEspnFromCookies({ league_id: '77' })
  assert.deepEqual(result, { ok: false, error: 'invalid_espn_cookies' })
})

// ── Source-read guards: enforce the migration off document.cookie / the JWT
// callback, and that no cookie value is ever logged. Comments legitimately
// reference the old approach when explaining the fix, so match against code
// with comments stripped. ──
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '') // block comments
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1') // line comments (leave URLs' https:// alone)
}
const swSrc = stripComments(readFileSync(swPath, 'utf-8'))
const authSrc = stripComments(readFileSync(authPath, 'utf-8'))

test('service worker relays to the extension endpoint, not the JWT callback', () => {
  assert.match(swSrc, /\/leagues\/connect\/espn\/extension/)
  assert.doesNotMatch(swSrc, /connect\/espn\/callback/)
})

test('service worker reads cookies via the cookies API, not document.cookie', () => {
  assert.match(swSrc, /browser\.cookies\.get/)
  assert.doesNotMatch(swSrc, /document\.cookie/)
})

test('espn_auth content script no longer reads cookie values', () => {
  assert.doesNotMatch(authSrc, /document\.cookie/)
  assert.doesNotMatch(authSrc, /espn_s2/)
})

test('cookie values are never logged', () => {
  assert.doesNotMatch(swSrc, /console\.\w+\([^)]*espn_s2/)
  assert.doesNotMatch(swSrc, /console\.\w+\([^)]*swid/i)
})
