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
  assert.match(src, /__draftmind_intercepting__/)
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
  assert.match(isolated, /detectEvents/)
  assert.match(isolated, /window\.__draftmind__\s*=\s*true/)
})

test('webpack builds the yahoo_draft_main entry', () => {
  assert.match(read('webpack.config.js'), /yahoo_draft_main:/)
})
