import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

// Source-read (api.js imports the browser shim, which needs extension globals).
const __dirname = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(__dirname, '..', 'src', 'utils', 'api.js'), 'utf-8')

test('getApiBase returns a URL ending in /api', () => {
  assert.match(src, /return\s+'https:\/\/[^']+\/api'/)
})

test('postDraftEvent posts to /draft/event via getApiBase (-> /api/draft/event)', () => {
  assert.match(src, /fetch\(`\$\{getApiBase\(\)\}\/draft\/event`/)
})
