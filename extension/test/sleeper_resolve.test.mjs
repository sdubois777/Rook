import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  parseFrame,
  isDraftFrame,
  draftIdFromTopic,
  draftIdFromPath,
  snakeSlot,
  roundOf,
  mySlotFrom,
  parseUserId,
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

test('draftIdFromPath: draft-room URLs across sports; null elsewhere', () => {
  // Drives the full-state sync even with ZERO frames flowing — the URL alone
  // is enough to recover the whole draft via the REST API.
  assert.equal(draftIdFromPath('/draft/nfl/1376707234477662208'), '1376707234477662208')
  assert.equal(draftIdFromPath('/draft/nfl/1376707234477662208/board'), '1376707234477662208')
  assert.equal(draftIdFromPath('/leagues/12345'), null)
  assert.equal(draftIdFromPath('/'), null)
  assert.equal(draftIdFromPath(null), null)
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

test('parseUserId: unwraps the JSON-encoded localStorage value to the bare id', () => {
  // The real Sleeper case: stored JSON-quoted.
  assert.equal(parseUserId('"1373225184038764544"'), '1373225184038764544')
  // Object form.
  assert.equal(parseUserId('{"user_id":"1373225184038764544"}'), '1373225184038764544')
  // Bare id — returned as-is, NOT JSON.parsed (a 19-digit id would lose precision).
  assert.equal(parseUserId('1373225184038764544'), '1373225184038764544')
  // Empty / missing.
  assert.equal(parseUserId(null), null)
  assert.equal(parseUserId(''), null)
})

test('parseUserId: bare id keeps full precision (no Number coercion)', () => {
  const id = '1373225184038764544' // > Number.MAX_SAFE_INTEGER
  assert.equal(parseUserId(id), id)
  assert.notEqual(parseUserId(id), String(Number(id))) // would be ...500 if coerced
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
// ---------------------------------------------------------------------------
// Orphaned-context recovery: an extension reload/update orphans the running
// content script (browser.* throws "Extension context invalidated"); the MAIN
// interceptor keeps posting frames but nothing relays. The poller must detect the
// dead context on a draft frame and reload the tab (capped) to re-inject a fresh,
// connected content script. (Static wiring assertion — behavior needs a browser.)
// ---------------------------------------------------------------------------
test('content script recovers from an invalidated extension context', () => {
  const __dirname = dirname(fileURLToPath(import.meta.url))
  const src = readFileSync(
    join(__dirname, '..', 'src', 'content_scripts', 'sleeper_draft.js'),
    'utf-8'
  )
  assert.match(src, /function extensionAlive\(\)/)
  assert.match(src, /browser\.runtime\.id/)
  assert.match(src, /location\.reload\(\)/)
  // recovery is gated on a real draft frame + a dead context, and capped to avoid
  // a reload loop when the extension is disabled.
  assert.match(src, /if \(!extensionAlive\(\)\) \{\s*\n\s*recoverInvalidatedContext\(\)/)
  assert.match(src, /n >= 2/)
})

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

// ---------------------------------------------------------------------------
// Real auction draft-start capture — the nominee/AI-rec blank was NOT the
// resolver (the backend/frontend wiring was); this replays the actual capture
// through detectFormat + detectAuctionEvents and asserts the nominations ARE
// emitted, so a future resolver regression is caught here.
// ---------------------------------------------------------------------------
const __dir = dirname(fileURLToPath(import.meta.url))

function detectFormatLocal(frame) {
  const t = frame.payload && frame.payload.type
  if (t === 'snake' || t === 'auction' || t === 'linear') return t === 'linear' ? 'snake' : t
  if (['new_draft_offer', 'draft_updated_by_offer', 'draft_updated_by_nomination'].includes(frame.event)) return 'auction'
  return null
}

test('auction capture: format locks at frame 0 and all 3 nominations emit', () => {
  const frames = JSON.parse(
    readFileSync(join(__dir, 'fixtures/sleeper/auction-draft-start.json'), 'utf8')
  )
  let format = null
  let lockedAt = null
  let mem = null
  const noms = []
  frames.forEach((arr, i) => {
    const frame = { joinRef: arr[0], ref: arr[1], topic: arr[2], event: arr[3], payload: arr[4] || {} }
    if (!/^draft:\d+$/.test(frame.topic || '')) return
    if (!format) { format = detectFormatLocal(frame); if (format) lockedAt = i }
    if (format !== 'auction') return
    if (!mem) mem = initAuctionMemory('u')
    const r = detectAuctionEvents(mem, frame)
    mem = r.next
    for (const ev of r.events) if (ev.type === 'nomination') noms.push(ev.payload.sleeper_player_id)
  })
  assert.equal(lockedAt, 0) // auction locks immediately on the first _by_* frame
  assert.deepEqual(noms, ['9493', '9221', '9488']) // real nominated_player_ids
})
