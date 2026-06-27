import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  parseFrame,
  isDraftFrame,
  draftIdFromTopic,
  snakeSlot,
  roundOf,
  mySlotFrom,
} from '../src/content_scripts/sleeper_resolve.mjs'
import { detectSnakeEvents, initSnakeMemory } from '../src/content_scripts/sleeper_snake_resolve.mjs'
import { detectAuctionEvents, initAuctionMemory } from '../src/content_scripts/sleeper_auction_resolve.mjs'

// ---------------------------------------------------------------------------
// Phoenix frame parsing
// ---------------------------------------------------------------------------
test('parseFrame: valid 5-tuple Phoenix frame', () => {
  const f = parseFrame('["1","2","draft:123","player_picked",{"pick_no":5}]')
  assert.equal(f.topic, 'draft:123')
  assert.equal(f.event, 'player_picked')
  assert.equal(f.payload.pick_no, 5)
})

test('parseFrame: rejects garbage / too-short / non-array', () => {
  assert.equal(parseFrame('not json'), null)
  assert.equal(parseFrame('[1,2]'), null)
  assert.equal(parseFrame('{"a":1}'), null)
  assert.equal(parseFrame(''), null)
})

test('isDraftFrame: only the draft:<id> channel (not presence/heartbeat)', () => {
  assert.equal(isDraftFrame(parseFrame('[null,null,"draft:42","player_picked",{}]')), true)
  assert.equal(isDraftFrame(parseFrame('[null,null,"presence_draft:42","presence_diff",{}]')), false)
  assert.equal(isDraftFrame(parseFrame('[null,"2","phoenix","heartbeat",{}]')), false)
  assert.equal(draftIdFromTopic('draft:42'), '42')
})

// ---------------------------------------------------------------------------
// Serpentine slot math
// ---------------------------------------------------------------------------
test('snakeSlot: round 1 forward, round 2 reverses (12-team)', () => {
  assert.equal(snakeSlot(1, 12), 1)
  assert.equal(snakeSlot(12, 12), 12)
  // round boundary: last of R1 picks again first in R2
  assert.equal(snakeSlot(13, 12), 12)
  assert.equal(snakeSlot(14, 12), 11)
  assert.equal(snakeSlot(24, 12), 1)
  assert.equal(snakeSlot(25, 12), 1) // R2 boundary → R3 forward again
})

test('snakeSlot: Nth-round reversal flips parity from that round on (rule-asserted)', () => {
  // reversal_round=3 → round 3 runs the SAME direction as round 2 (reverse).
  assert.equal(snakeSlot(25, 12, 3), 12) // R3 pick 1 → reversed → slot 12
  assert.equal(snakeSlot(25, 12, 0), 1) // standard → slot 1
})

test('roundOf + mySlotFrom', () => {
  assert.equal(roundOf(13, 12), 2)
  assert.equal(mySlotFrom({ '1373225184038764544': 4 }, '1373225184038764544'), 4)
  assert.equal(mySlotFrom({ '999': 4 }, '1373225184038764544'), null)
})

// ---------------------------------------------------------------------------
// CROSS-POLLER: the Sleeper resolvers IGNORE non-draft frames (presence,
// heartbeat, garbage). Platform isolation otherwise is by host (manifest):
// sleeper_draft.js only injects on sleeper.com/sleeper.app, and the Yahoo/ESPN
// pollers never run there. There is no shared page, so no DOM cross-fire.
// ---------------------------------------------------------------------------
test('cross-poller: snake/auction resolvers emit nothing for non-draft frames', () => {
  const nonDraft = [
    parseFrame('[null,null,"presence_draft:1","presence_diff",{"joins":{}}]'),
    parseFrame('[null,"2","phoenix","heartbeat",{}]'),
    parseFrame('garbage'), // null
  ]
  for (const f of nonDraft) {
    assert.equal(detectSnakeEvents(initSnakeMemory('1'), f).events.length, 0)
    assert.equal(detectAuctionEvents(initAuctionMemory('1'), f).events.length, 0)
  }
})
