import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import {
  parseSnakeState,
  detectSnakeEvents,
} from '../src/content_scripts/yahoo_snake_draft_observer.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const manifest = JSON.parse(
  readFileSync(join(__dirname, '..', 'manifest.json'), 'utf-8')
)

// Representative #app innerText snapshots from a live June 2026 snake session.
const SOMEONE_ELSE = [
  "Bart's Pick • You're up in 9 Picks • Round 7, Pick 84",
  'Last:',
  'J. DOBBINS',
  '(RB · DEN)',
  'Available Players',
  'R. Pearsall',
].join('\n')

const YOUR_TURN = ['YOUR TURN • ROUND 8, PICK 93', 'Last:', 'J. DOBBINS', '(RB · DEN)'].join(
  '\n'
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

test('parseSnakeState detects YOUR TURN', () => {
  const s = parseSnakeState(YOUR_TURN)
  assert.equal(s.isYourTurn, true)
  assert.equal(s.picksUntilYourTurn, 0)
})

test('parseSnakeState extracts round and pick when you are up', () => {
  const s = parseSnakeState(YOUR_TURN)
  assert.equal(s.yourRound, 8)
  assert.equal(s.yourPick, 93)
  assert.equal(s.currentRound, 8)
  assert.equal(s.currentPick, 93)
})

test('parseSnakeState detects who is picking and picks until your turn', () => {
  const s = parseSnakeState(SOMEONE_ELSE)
  assert.equal(s.isYourTurn, false)
  assert.equal(s.currentPicker, 'Bart')
  assert.equal(s.currentRound, 7)
  assert.equal(s.currentPick, 84)
  assert.equal(s.picksUntilYourTurn, 9)
})

test('parseSnakeState extracts the last pick', () => {
  const s = parseSnakeState(SOMEONE_ELSE)
  assert.deepEqual(s.lastPick, {
    player_name: 'J. DOBBINS',
    position: 'RB',
    team: 'DEN',
  })
})

test('parseSnakeState returns null on empty text', () => {
  assert.equal(parseSnakeState(''), null)
  assert.equal(parseSnakeState(null), null)
})

test('detectSnakeEvents fires your_turn on the rising edge only', () => {
  const start = { wasYourTurn: false, lastPicksUntil: null }
  const curr = parseSnakeState(YOUR_TURN)

  const first = detectSnakeEvents(start, curr)
  assert.equal(first.events.length, 1)
  assert.equal(first.events[0].type, 'your_turn')
  assert.deepEqual(first.events[0].payload, {
    round: 8,
    pick: 93,
    picks_until_your_turn: 0,
  })

  // Still your turn next tick — no duplicate event.
  const second = detectSnakeEvents(first.next, curr)
  assert.equal(second.events.length, 0)
})

test('detectSnakeEvents fires your_turn_soon once at 2 picks away', () => {
  const twoAway = parseSnakeState(
    "Ann's Pick • You're up in 2 Picks • Round 7, Pick 82"
  )
  const start = { wasYourTurn: false, lastPicksUntil: 3 }
  const r = detectSnakeEvents(start, twoAway)
  assert.equal(r.events.length, 1)
  assert.equal(r.events[0].type, 'your_turn_soon')
  assert.equal(r.events[0].payload.picks_until_your_turn, 2)

  // Same 2-away state again — no repeat.
  const again = detectSnakeEvents(r.next, twoAway)
  assert.equal(again.events.length, 0)
})

test('detectSnakeEvents emits nothing when far from your pick', () => {
  const far = parseSnakeState(
    "Bart's Pick • You're up in 9 Picks • Round 7, Pick 84"
  )
  const r = detectSnakeEvents({ wasYourTurn: false, lastPicksUntil: null }, far)
  assert.equal(r.events.length, 0)
})
