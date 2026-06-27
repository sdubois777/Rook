import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parseFrame } from '../src/content_scripts/sleeper_resolve.mjs'
import { initSnakeMemory, detectSnakeEvents } from '../src/content_scripts/sleeper_snake_resolve.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRAMES = JSON.parse(
  readFileSync(join(__dirname, 'fixtures', 'sleeper', 'snake.json'), 'utf-8')
)
const MY_USER = '1373225184038764544' // → slot 4 in this fixture

/** Replay all frames through the resolver, collecting every emitted event. */
function replay(frames, myUser) {
  let mem = initSnakeMemory(myUser)
  const events = []
  for (const arr of frames) {
    const { events: evs, next } = detectSnakeEvents(mem, parseFrame(JSON.stringify(arr)))
    mem = next
    events.push(...evs)
  }
  return events
}

// inline frame builders for crafted turn scenarios
const cfg = (order) => [null, null, 'draft:9', 'draft_updated_by_pick',
  { type: 'snake', status: 'drafting', settings: { teams: 12, reversal_round: 0 }, draft_order: order }]
const pick = (n) => [null, null, 'draft:9', 'player_picked',
  { player_id: `p${n}`, pick_no: n, metadata: { first_name: 'P', last_name: String(n), position: 'RB', team: 'X' } }]

test('snake_pick: parses player/team/position/sleeper_id + serpentine picker', () => {
  const picks = replay(FRAMES, MY_USER).filter((e) => e.type === 'snake_pick')
  assert.equal(picks.length, 7) // picks 5..11
  const p5 = picks.find((e) => e.payload.pick_number === 5)
  assert.deepEqual(p5.payload, {
    pick_number: 5,
    player_name: 'Jaxon Smith-Njigba',
    position: 'WR',
    nfl_team: 'SEA',
    sleeper_player_id: '9488',
    picker: 'Team 5', // serpentine slot for pick 5 (round 1)
    is_yours: false,
    round: 1,
  })
})

test('snake_pick: deduped by pick_no on replay', () => {
  const once = replay(FRAMES, MY_USER).filter((e) => e.type === 'snake_pick').length
  // feed the whole stream twice through one memory → no double-emit
  let mem = initSnakeMemory(MY_USER)
  const all = []
  for (const arr of [...FRAMES, ...FRAMES]) {
    const r = detectSnakeEvents(mem, parseFrame(JSON.stringify(arr)))
    mem = r.next
    all.push(...r.events)
  }
  assert.equal(all.filter((e) => e.type === 'snake_pick').length, once)
})

test('snake_status: current pick/round + countdown advance with the stream', () => {
  const status = replay(FRAMES, MY_USER).filter((e) => e.type === 'snake_status')
  // after pick 11 → current pick 12, my (slot 4) next pick is 21 → 9 away
  const last = status[status.length - 1]
  assert.equal(last.payload.current_pick, 12)
  assert.equal(last.payload.current_round, 1)
  assert.equal(last.payload.picks_until_your_turn, 9)
})

test('your_turn fires when my slot comes on the clock', () => {
  // mySlot 5; after pick 4, current pick 5 == my slot → your_turn
  let mem = initSnakeMemory(MY_USER)
  const out = []
  for (const arr of [cfg({ [MY_USER]: 5 }), pick(4)]) {
    const r = detectSnakeEvents(mem, parseFrame(JSON.stringify(arr)))
    mem = r.next
    out.push(...r.events)
  }
  const yt = out.find((e) => e.type === 'your_turn')
  assert.ok(yt, 'your_turn emitted')
  assert.deepEqual(yt.payload, { round: 1, pick: 5, picks_until_your_turn: 0 })
})

test('your_turn_soon fires at exactly 2 picks away', () => {
  // mySlot 5; after pick 2, current pick 3, my next pick 5 → 2 away
  let mem = initSnakeMemory(MY_USER)
  const out = []
  for (const arr of [cfg({ [MY_USER]: 5 }), pick(2)]) {
    const r = detectSnakeEvents(mem, parseFrame(JSON.stringify(arr)))
    mem = r.next
    out.push(...r.events)
  }
  const soon = out.find((e) => e.type === 'your_turn_soon')
  assert.ok(soon)
  assert.equal(soon.payload.picks_until_your_turn, 2)
})

test('is_yours tags my own pick', () => {
  // mySlot 5, pick 5 is mine
  let mem = initSnakeMemory(MY_USER)
  const out = []
  for (const arr of [cfg({ [MY_USER]: 5 }), pick(5)]) {
    const r = detectSnakeEvents(mem, parseFrame(JSON.stringify(arr)))
    mem = r.next
    out.push(...r.events)
  }
  const sp = out.find((e) => e.type === 'snake_pick')
  assert.equal(sp.payload.is_yours, true)
  assert.equal(sp.payload.picker, 'Team 5')
})
