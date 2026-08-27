/**
 * Recovery from an orphaned content script (#461).
 *
 * When Chrome auto-updates the extension, content scripts already running in open
 * tabs are orphaned: every browser.* call throws, so the reader keeps parsing the
 * page but nothing it reads can reach the backend again — and it says nothing.
 * The draft silently stops relaying for the rest of the session.
 *
 * A Pro customer hit exactly this: their draft session recorded 86 of ~160 picks
 * and their own picks began at ROUND 7, with rounds 1-6 never captured. Their plan
 * was fine and their events were being accepted, so nothing refused them.
 *
 * This was implemented for Sleeper only, back when the extension was sideloaded and
 * could not auto-update. It is now on the Chrome Web Store, so this is a live risk
 * for every reader. These are BEHAVIOUR tests of the shared module the ESPN, Yahoo
 * and Sleeper readers all use — the previous coverage was a source-text assertion
 * on one file, which could not catch a logic change.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

// The browser shim captures globalThis.chrome AT IMPORT TIME, so the global must
// exist before the module under test is loaded. Held as one object whose
// `runtime` is swapped, so "the extension died" is expressible after import.
const chromeStub = { runtime: { id: 'extension-alive' } }
globalThis.chrome = chromeStub

let reloads = 0
const store = new Map()
globalThis.sessionStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
}
globalThis.location = { reload: () => { reloads += 1 } }

const {
  extensionAlive,
  guardTick,
  noteHealthyRelay,
  recoverInvalidatedContext,
  resetContextReloadCap,
  MAX_CONTEXT_RELOADS,
} = await import('../src/utils/context_recovery.js')

function reset({ alive }) {
  store.clear()
  reloads = 0
  chromeStub.runtime = alive ? { id: 'extension-alive' } : undefined
}

// ---------------------------------------------------------------------------
// Liveness
// ---------------------------------------------------------------------------
test('a healthy context is detected as alive', () => {
  reset({ alive: true })
  assert.equal(extensionAlive(), true)
})

test('an orphaned context is detected as dead, without throwing', () => {
  // The whole failure mode is that browser.* THROWS once orphaned. The probe
  // itself must not, or the reader dies before it can recover.
  reset({ alive: false })
  assert.equal(extensionAlive(), false)
})

// ---------------------------------------------------------------------------
// The tick guard — what the ESPN and Yahoo readers call
// ---------------------------------------------------------------------------
test('a healthy tick proceeds and never reloads the page', () => {
  reset({ alive: true })
  assert.equal(guardTick('ESPN'), false)   // false = carry on with the tick
  assert.equal(reloads, 0)
})

test('an orphaned tick stops the reader and reloads once to re-inject it', () => {
  reset({ alive: false })
  assert.equal(guardTick('ESPN'), true)    // true = abandon this tick
  assert.equal(reloads, 1)
})

// ---------------------------------------------------------------------------
// The cap — recover, but never loop
// ---------------------------------------------------------------------------
test('reloads are capped, then it warns instead of looping', () => {
  // A disabled or broken extension must not put the tab in a reload loop.
  reset({ alive: false })
  const warnings = []
  const realWarn = console.warn
  console.warn = (m) => warnings.push(String(m))
  try {
    for (let i = 0; i < MAX_CONTEXT_RELOADS + 3; i++) recoverInvalidatedContext('ESPN')
  } finally {
    console.warn = realWarn
  }
  assert.equal(reloads, MAX_CONTEXT_RELOADS)
  assert.ok(warnings.length >= 1)
  // The warning has to tell the user the one thing that fixes it.
  assert.match(warnings[0], /refresh this ESPN tab/)
})

test('the platform name appears in the warning, so it names the right tab', () => {
  reset({ alive: false })
  store.set('rook_ctx_reloads', String(MAX_CONTEXT_RELOADS))
  const warnings = []
  const realWarn = console.warn
  console.warn = (m) => warnings.push(String(m))
  try {
    recoverInvalidatedContext('Yahoo')
  } finally {
    console.warn = realWarn
  }
  assert.match(warnings[0], /refresh this Yahoo tab/)
  assert.equal(reloads, 0)                 // capped — no reload attempted
})

// ---------------------------------------------------------------------------
// Resetting the cap — the bug the Sleeper version was fixed for once already
// ---------------------------------------------------------------------------
test('a fresh injection clears the cap, so the cap is per-episode not per-tab', () => {
  // The earlier version reset only on a healthy RELAY, so a tab that sat in a
  // lobby through two extension reloads exhausted the cap permanently and the
  // next orphaning only printed a warning.
  reset({ alive: true })
  store.set('rook_ctx_reloads', String(MAX_CONTEXT_RELOADS))
  resetContextReloadCap()
  assert.equal(store.get('rook_ctx_reloads'), '0')

  chromeStub.runtime = undefined           // orphaned again, later
  assert.equal(guardTick('ESPN'), true)
  assert.equal(reloads, 1)                 // gets a fresh attempt
})

test('a dead context does NOT clear the cap at startup', () => {
  // Otherwise an orphaned script could clear its own cap and loop forever.
  reset({ alive: false })
  store.set('rook_ctx_reloads', String(MAX_CONTEXT_RELOADS))
  resetContextReloadCap()
  assert.equal(store.get('rook_ctx_reloads'), String(MAX_CONTEXT_RELOADS))
})

test('an accepted relay clears the cap', () => {
  reset({ alive: true })
  store.set('rook_ctx_reloads', '1')
  noteHealthyRelay()
  assert.equal(store.get('rook_ctx_reloads'), '0')
})

// ---------------------------------------------------------------------------
// Blocked storage must degrade, not crash
// ---------------------------------------------------------------------------
test('sessionStorage being blocked still allows a reload attempt', () => {
  reset({ alive: false })
  const realStorage = globalThis.sessionStorage
  globalThis.sessionStorage = {
    getItem: () => { throw new Error('blocked') },
    setItem: () => { throw new Error('blocked') },
  }
  try {
    assert.equal(guardTick('ESPN'), true)
    assert.equal(reloads, 1)
  } finally {
    globalThis.sessionStorage = realStorage
  }
})
