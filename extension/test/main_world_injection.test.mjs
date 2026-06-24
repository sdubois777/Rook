import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (p) => readFileSync(join(ROOT, p), 'utf-8')
const manifest = () => JSON.parse(read('manifest.json'))

const DRAFT_MATCH = 'https://football.fantasysports.yahoo.com/draftclient/*'

test('yahoo_draft_main.js exists and intercepts console.error', () => {
  const src = read('src/content_scripts/yahoo_draft_main.js')
  assert.match(src, /console\.error/)
  assert.match(src, /__yahoo_draft_action__/)
  assert.match(src, /__rook_intercepting__/)
})

test('manifest injects yahoo_draft_main.js into the MAIN world for the draft room', () => {
  const entry = manifest().content_scripts.find(
    (cs) => Array.isArray(cs.js) && cs.js.includes('yahoo_draft_main.js')
  )
  assert.ok(entry, 'a content_scripts entry must inject yahoo_draft_main.js')
  assert.equal(entry.world, 'MAIN')
  assert.equal(entry.run_at, 'document_start')
  assert.ok(
    entry.matches.includes(DRAFT_MATCH),
    'main-world script must match the draft room URL'
  )
})

test('main-world injection uses a file, not inline script (CSP-safe)', () => {
  const src = read('src/content_scripts/yahoo_draft.js')
  // The isolated content script must no longer build an inline <script> tag —
  // that is exactly what Yahoo's CSP blocks. Injection is manifest-declared.
  assert.doesNotMatch(src, /injectMainWorldScript/)
  assert.doesNotMatch(src, /createElement\(\s*['"]script['"]\s*\)/)
  assert.doesNotMatch(src, /\.textContent\s*=/)
})

test('manifest does not rely on web_accessible_resources for injection', () => {
  // A declared MAIN-world content script is injected by the browser, so it is
  // NOT page-fetched and needs no web_accessible_resources entry.
  const war = manifest().web_accessible_resources || []
  const exposesMain = war.some(
    (r) => Array.isArray(r.resources) && r.resources.includes('yahoo_draft_main.js')
  )
  assert.equal(exposesMain, false)
})

test('DOM poller core is independent of the MAIN-world interceptor', () => {
  // The poller (pure parse/detect logic) must not depend on console.error
  // interception — it works even if the MAIN-world script never loads.
  const parse = read('src/content_scripts/yahoo_draft_parse.mjs')
  assert.doesNotMatch(parse, /console\.error/)
  assert.doesNotMatch(parse, /__yahoo_draft_action__/)

  const isolated = read('src/content_scripts/yahoo_draft.js')
  // React-client core: resolve the board + diff events, no MAIN-world dependency.
  assert.match(isolated, /resolveAuctionState/)
  assert.match(isolated, /detectAuctionEvents/)
  assert.match(isolated, /window\.__rook__\s*=\s*true/)
})

test('webpack builds the yahoo_draft_main entry', () => {
  assert.match(read('webpack.config.js'), /yahoo_draft_main:/)
})

// --- Snake MAIN-world injection (Yahoo CSP blocks the ISOLATED-only path) ---

test('yahoo_snake_draft_main.js intercepts console.error and sets the flag', () => {
  const src = read('src/content_scripts/yahoo_snake_draft_main.js')
  assert.match(src, /console\.error/)
  // The '0' frame is now a content-free trigger, not a data carrier.
  assert.match(src, /__yahoo_pick_made__/)
  assert.match(src, /window\.__rook_snake__\s*=\s*true/)
  // Snake picks are the '0' frame.
  assert.match(src, /\[0\]\s*===\s*'0'/)
})

test('manifest injects yahoo_snake_draft_main.js into the MAIN world', () => {
  const entry = manifest().content_scripts.find(
    (cs) => Array.isArray(cs.js) && cs.js.includes('yahoo_snake_draft_main.js')
  )
  assert.ok(entry, 'a content_scripts entry must inject yahoo_snake_draft_main.js')
  assert.equal(entry.world, 'MAIN')
  assert.equal(entry.run_at, 'document_start')
  assert.ok(entry.matches.includes(DRAFT_MATCH))
})

test('yahoo_snake_draft.js (DOM poller) stays in the ISOLATED world', () => {
  const entry = manifest().content_scripts.find(
    (cs) => Array.isArray(cs.js) && cs.js.includes('yahoo_snake_draft.js')
  )
  assert.ok(entry, 'snake DOM poller entry must exist')
  // ISOLATED is the default — the poller must NOT be in the MAIN world.
  assert.notEqual(entry.world, 'MAIN')
})

test('snake poller listens for the MAIN-world pick trigger, not direct console.error', () => {
  const src = read('src/content_scripts/yahoo_snake_draft.js')
  assert.match(src, /__yahoo_pick_made__/)
  // It must NOT wrap console.error itself (it's in the ISOLATED world).
  assert.doesNotMatch(src, /console\.error\s*=/)
})

test('snake main IIFE fires the pick trigger on a 0 frame and sets the flag', () => {
  const src = read('src/content_scripts/yahoo_snake_draft_main.js')
  const dispatched = []
  const fakeWin = { dispatchEvent: (e) => dispatched.push(e) }
  const fakeConsole = { error: () => {} }
  class FakeCustomEvent {
    constructor(type, init) {
      this.type = type
      this.detail = init && init.detail
    }
  }
  // Execute the IIFE with injected globals (no real browser needed).
  new Function('window', 'console', 'CustomEvent', src)(fakeWin, fakeConsole, FakeCustomEvent)

  assert.equal(fakeWin.__rook_snake__, true)

  // A '0' frame fires a content-free trigger (the poller reads the React board).
  fakeConsole.error(['0', 'lg', 'dr', 84, 'nfl.p.1'])
  assert.equal(dispatched.length, 1)
  assert.equal(dispatched[0].type, '__yahoo_pick_made__')

  // Unrelated console.error output is NOT forwarded.
  fakeConsole.error('a normal error string')
  fakeConsole.error(['B', 'lg', 'dr', 'p', 5]) // auction bid, not ours to handle
  assert.equal(dispatched.length, 1)
})

test('webpack builds the yahoo_snake_draft_main entry', () => {
  assert.match(read('webpack.config.js'), /yahoo_snake_draft_main:/)
})
