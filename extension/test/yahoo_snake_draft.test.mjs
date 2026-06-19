import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import {
  parseSnakeState,
  detectSnakeEvents,
  parsePicks,
} from '../src/content_scripts/yahoo_snake_draft_observer.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const manifest = JSON.parse(
  readFileSync(join(__dirname, '..', 'manifest.json'), 'utf-8')
)

// Representative #app innerText snapshots from a live June 2026 snake session.
const SOMEONE_ELSE = [
  "Bart's Pick • You're up in 9 Picks • Round 7, Pick 84",
].join('\n')

const YOUR_TURN = ['YOUR TURN • ROUND 8, PICK 93'].join('\n')

// Confirmed "Picks" panel structure: pick# / team / player / position / nfl,
// then 1-4 upcoming-order team names (noise) before the next pick number.
const PICKS_PANEL = [
  'Draft Room',
  'Picks',
  '1',
  'You',
  'J. Chase',
  'WR',
  'Cin',
  'Quy', // noise (upcoming order)
  'Nick', // noise
  '2',
  'Quy',
  'B. Robinson',
  'RB',
  'Atl',
  'Mike joined', // joined noise — must be filtered
  '3',
  'Nick',
  'J. Cook III',
  'RB',
  'Buf',
  '4', // snake reversal: Nick picks again back-to-back
  'Nick',
  'P. Nacua',
  'WR',
  'LAR',
  'Bart left', // left noise — must be filtered
].join('\n')

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

// --- parseSnakeState (turn detection — unchanged) ---------------------------

test('parseSnakeState detects YOUR TURN', () => {
  const s = parseSnakeState(YOUR_TURN)
  assert.equal(s.isYourTurn, true)
  assert.equal(s.picksUntilYourTurn, 0)
})

test('parseSnakeState extracts round and pick when you are up', () => {
  const s = parseSnakeState(YOUR_TURN)
  assert.equal(s.yourRound, 8)
  assert.equal(s.yourPick, 93)
})

test('parseSnakeState detects who is picking and picks until your turn', () => {
  const s = parseSnakeState(SOMEONE_ELSE)
  assert.equal(s.isYourTurn, false)
  assert.equal(s.currentPicker, 'Bart')
  assert.equal(s.currentRound, 7)
  assert.equal(s.currentPick, 84)
  assert.equal(s.picksUntilYourTurn, 9)
})

test('parseSnakeState returns null on empty text', () => {
  assert.equal(parseSnakeState(''), null)
  assert.equal(parseSnakeState(null), null)
})

// --- detectSnakeEvents (turn/countdown ONLY now) ----------------------------

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

  const again = detectSnakeEvents(r.next, twoAway)
  assert.equal(again.events.length, 0)
})

test('detectSnakeEvents no longer emits pick events (parsePicks owns those)', () => {
  const r = detectSnakeEvents(
    { wasYourTurn: false, lastPicksUntil: null },
    parseSnakeState(SOMEONE_ELSE)
  )
  assert.equal(r.events.filter((e) => e.type === 'snake_pick').length, 0)
})

// --- parsePicks (the complete Picks-panel history) --------------------------

test('parsePicks extracts pick 1 correctly', () => {
  const picks = parsePicks(PICKS_PANEL)
  assert.deepEqual(picks[0], {
    pick_number: 1,
    team: 'You',
    player_name: 'J. Chase',
    position: 'WR',
    nfl_team: 'Cin',
    is_yours: true,
  })
})

test('parsePicks marks YOUR team (You) is_yours true', () => {
  assert.equal(parsePicks(PICKS_PANEL)[0].is_yours, true)
})

test('parsePicks marks an opponent is_yours false', () => {
  const quy = parsePicks(PICKS_PANEL).find((p) => p.team === 'Quy')
  assert.equal(quy.is_yours, false)
})

test('parsePicks filters joined/left noise (4 real picks)', () => {
  const picks = parsePicks(PICKS_PANEL)
  assert.equal(picks.length, 4)
  assert.ok(!picks.some((p) => p.player_name.includes('joined')))
  assert.ok(!picks.some((p) => p.player_name.includes('left')))
})

test('parsePicks skips the upcoming-order lines between picks', () => {
  // Quy/Nick appear as noise after pick 1 but must not become picks.
  const nums = parsePicks(PICKS_PANEL).map((p) => p.pick_number)
  assert.deepEqual(nums, [1, 2, 3, 4])
})

test('parsePicks handles back-to-back teams (snake reversal)', () => {
  const picks = parsePicks(PICKS_PANEL)
  assert.equal(picks[2].team, 'Nick')
  assert.equal(picks[3].team, 'Nick') // Nick picks again at the turn
})

test('parsePicks preserves a III suffix in the player name', () => {
  const cook = parsePicks(PICKS_PANEL).find((p) => p.pick_number === 3)
  assert.equal(cook.player_name, 'J. Cook III')
})

test('parsePicks returns [] when there is no Picks header', () => {
  assert.deepEqual(parsePicks('Draft Room\nfoo\nbar'), [])
  assert.deepEqual(parsePicks(''), [])
})

test('parsePicks ignores a number not followed by a valid position', () => {
  // "5 / Quy / Something / Bye 9 / ..." — "Bye 9" is filtered, but even a junk
  // position field must not yield a pick.
  const junk = ['Picks', '5', 'Quy', 'Some Name', 'NOTAPOS', 'Atl'].join('\n')
  assert.deepEqual(parsePicks(junk), [])
})
