import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import {
  initSnakeObserver,
  snapshotDom,
} from '../src/content_scripts/yahoo_snake_draft_observer.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const manifest = JSON.parse(
  readFileSync(join(__dirname, '..', 'manifest.json'), 'utf-8')
)

test('yahoo_snake_draft.js in manifest matches the Yahoo draft URL', () => {
  const cs = manifest.content_scripts.find((c) =>
    c.js.includes('yahoo_snake_draft.js')
  )
  assert.ok(cs, 'snake content script missing from manifest')
  assert.ok(
    cs.matches.some((m) => m.includes('football.fantasysports.yahoo.com')),
    'snake content script does not match the Yahoo draft domain'
  )
})

test('snake stub sets __draftmind_snake__ and is idempotent', () => {
  const win = { console: { log: () => {} }, setInterval: () => 0 }
  const doc = { querySelector: () => null }

  assert.equal(initSnakeObserver(win, doc), true)
  assert.equal(win.__draftmind_snake__, true)
  // Second activation is a no-op (already running).
  assert.equal(initSnakeObserver(win, doc), false)
})

test('snapshotDom returns null when no draft container', () => {
  assert.equal(snapshotDom({ querySelector: () => null }), null)
})

test('snapshotDom truncates to 500 chars', () => {
  const el = { innerText: 'x'.repeat(1000) }
  const snap = snapshotDom({ querySelector: () => el })
  assert.equal(snap.length, 500)
})
