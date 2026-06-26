import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parseHTML } from 'linkedom'

import { isSalaryCap } from '../src/content_scripts/espn_salarycap_resolve.mjs'
import { isSnake } from '../src/content_scripts/espn_snake_resolve.mjs'
import { shouldAuctionActivate } from '../src/content_scripts/yahoo_auction_resolve.mjs'
import { shouldSnakeActivate, snakeRoot } from '../src/content_scripts/yahoo_snake_resolve.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FIX = join(__dirname, 'fixtures')
function docFor(rel) {
  return parseHTML(readFileSync(join(FIX, rel), 'utf-8')).document
}

const ESPN_FIXTURES = [
  'espn/salarycap/lobby.html',
  'espn/salarycap/nomination-active.html',
  'espn/salarycap/sale.html',
  'espn/salarycap/board-mid.html',
  'espn/snake/on-the-clock.html',
  'espn/snake/board-mid.html',
]
const YAHOO_FIXTURES = [
  'auction/lobby.html',
  'auction/nomination.html',
  'auction/your-turn.html',
  'auction/snake-onclock.html',
  'auction/snake-waiting.html',
]

// ESPN pollers must stay INERT on every Yahoo fixture (no ESPN testids there).
test('ESPN pollers inert on every Yahoo fixture', () => {
  for (const f of YAHOO_FIXTURES) {
    const d = docFor(f)
    assert.equal(isSalaryCap(d), false, `ESPN salary-cap must be inert on ${f}`)
    assert.equal(isSnake(d), false, `ESPN snake must be inert on ${f}`)
  }
})

// Yahoo pollers must stay INERT on every ESPN fixture (no Yahoo React root /
// ys-* hooks there).
test('Yahoo pollers inert on every ESPN fixture', () => {
  for (const f of ESPN_FIXTURES) {
    const d = docFor(f)
    assert.equal(shouldAuctionActivate(d), false, `Yahoo auction must be inert on ${f}`)
    assert.equal(shouldSnakeActivate(snakeRoot(d)), false, `Yahoo snake must be inert on ${f}`)
  }
})

// SAME-PASS positive control: each platform's gate fires on its OWN content while
// the other platform's gates stay quiet — proving the split, not just silence.
test('SAME-PASS: ESPN active on ESPN, Yahoo active on Yahoo, no cross-fire', () => {
  // ESPN salary-cap fixture: ESPN salary-cap on, everything else off.
  const sc = docFor('espn/salarycap/nomination-active.html')
  assert.equal(isSalaryCap(sc), true)
  assert.equal(isSnake(sc), false)
  assert.equal(shouldAuctionActivate(sc), false)
  assert.equal(shouldSnakeActivate(snakeRoot(sc)), false)

  // ESPN snake fixture: ESPN snake on, everything else off.
  const sn = docFor('espn/snake/board-mid.html')
  assert.equal(isSnake(sn), true)
  assert.equal(isSalaryCap(sn), false)
  assert.equal(shouldAuctionActivate(sn), false)
  assert.equal(shouldSnakeActivate(snakeRoot(sn)), false)

  // Yahoo auction fixture: Yahoo auction on, ESPN gates off.
  const ya = docFor('auction/lobby.html')
  assert.equal(shouldAuctionActivate(ya), true)
  assert.equal(isSalaryCap(ya), false)
  assert.equal(isSnake(ya), false)

  // Yahoo snake fixture: Yahoo snake on, ESPN gates off.
  const ys = docFor('auction/snake-onclock.html')
  assert.equal(shouldSnakeActivate(snakeRoot(ys)), true)
  assert.equal(isSalaryCap(ys), false)
  assert.equal(isSnake(ys), false)
})
