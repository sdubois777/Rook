import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parseHTML } from 'linkedom'

import {
  isSnake,
  resolvePicklist,
  resolveSnakeState,
  detectSnakeEvents,
  initSnakeMemory,
} from '../src/content_scripts/espn_snake_resolve.mjs'
import { isSalaryCap } from '../src/content_scripts/espn_salarycap_resolve.mjs'
import { stripDarkreader, resolveMyTeam } from '../src/content_scripts/espn_shared.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ESPN = join(__dirname, 'fixtures', 'espn')
const SNAKE = join(ESPN, 'snake')

function docFor(name) {
  const { document } = parseHTML(readFileSync(join(SNAKE, name), 'utf-8'))
  stripDarkreader(document)
  return document
}
const MY_TEAM = "Stephen's Smart Team"

// ---------------------------------------------------------------------------
// Gate — live snake picking states only (current-pick, no auction-pick).
// lobby/complete have no current-pick → inert (pre/post draft, nothing to poll).
// ---------------------------------------------------------------------------
test('gate: snake active on live picking states; salary-cap gate inert', () => {
  for (const f of ['on-the-clock', 'your-turn-soon', 'post-pick', 'board-mid']) {
    const d = docFor(`${f}.html`)
    assert.equal(isSnake(d), true, `snake active on ${f}`)
    assert.equal(isSalaryCap(d), false, `salary-cap inert on snake ${f}`)
  }
  // pre/post-draft have no on-the-clock pick → not a live poll target
  assert.equal(isSnake(docFor('lobby.html')), false)
  assert.equal(isSnake(docFor('complete.html')), false)
})

// ---------------------------------------------------------------------------
// Self-team + board column mapping (from the full board fixtures)
// ---------------------------------------------------------------------------
test('self-team resolves from the board .myTeam header', () => {
  assert.equal(resolveMyTeam(docFor('board-mid.html')), MY_TEAM)
})

// ---------------------------------------------------------------------------
// Status widget (partial captures) — current pick / on-clock / picklist
// ---------------------------------------------------------------------------
test('status: on-the-clock = my team → your_turn', () => {
  const st = resolveSnakeState(docFor('on-the-clock.html'))
  assert.equal(st.currentPick, 11)
  assert.equal(st.onClockTeam, MY_TEAM)
  assert.equal(st.isYourTurn, true)
  assert.equal(st.round, 1)
})

test('status: opponent on the clock, my next pick is 2 away', () => {
  const st = resolveSnakeState(docFor('your-turn-soon.html'))
  assert.equal(st.currentPick, 9)
  assert.equal(st.onClockTeam, 'Team 9')
  assert.equal(st.isYourTurn, false)
  assert.equal(st.picksUntil, 2) // my next picklist pick (11) − current (9)
})

test('picklist: upcoming pick numbers + teams', () => {
  const pl = resolvePicklist(docFor('your-turn-soon.html'))
  assert.equal(pl[0].pickNum, 10)
  assert.equal(pl[1].team, MY_TEAM)
})

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------
test('your_turn fires on the rising edge only', () => {
  const st = resolveSnakeState(docFor('on-the-clock.html'))
  const first = detectSnakeEvents(initSnakeMemory(), st)
  const yt = first.events.find((e) => e.type === 'your_turn')
  assert.ok(yt)
  assert.deepEqual(yt.payload, { round: 1, pick: 11, picks_until_your_turn: 0 })
  assert.equal(detectSnakeEvents(first.next, st).events.some((e) => e.type === 'your_turn'), false)
})

test('ALL snake_picks relay BEFORE your_turn in the same tick', () => {
  // The pick right before your turn lands in the same tick as the your-turn
  // signal. Picks must relay FIRST so the backend records them before
  // generating the recommendation — else the engine recommends a just-drafted
  // player (the McConkey bug).
  const board = resolveSnakeState(docFor('board-mid.html'))
  const curr = { ...board, isYourTurn: true, picksUntil: 0 }
  const { events } = detectSnakeEvents(initSnakeMemory(), curr)
  const types = events.map((e) => e.type)
  const lastPickIdx = types.lastIndexOf('snake_pick')
  const turnIdx = types.indexOf('your_turn')
  assert.ok(lastPickIdx >= 0, 'board-mid has completed picks')
  assert.ok(turnIdx >= 0, 'your_turn fires')
  assert.ok(lastPickIdx < turnIdx, 'every snake_pick relays before your_turn')
})

test('your_turn_soon fires once at exactly 2 away', () => {
  const st = resolveSnakeState(docFor('your-turn-soon.html'))
  const start = { ...initSnakeMemory(), lastPicksUntil: 3 }
  const r = detectSnakeEvents(start, st)
  const soon = r.events.find((e) => e.type === 'your_turn_soon')
  assert.ok(soon)
  assert.equal(soon.payload.picks_until_your_turn, 2)
  assert.equal(detectSnakeEvents(r.next, st).events.some((e) => e.type === 'your_turn_soon'), false)
})

test('snake_status carries pick/round/countdown, deduped then re-fires on change', () => {
  const a = resolveSnakeState(docFor('your-turn-soon.html'))
  const r1 = detectSnakeEvents(initSnakeMemory(), a)
  const s1 = r1.events.find((e) => e.type === 'snake_status')
  assert.equal(s1.payload.current_pick, 9)
  assert.equal(s1.payload.current_round, 1)
  assert.equal(s1.payload.picks_until_your_turn, 2)
  assert.equal(s1.payload.your_team_name, MY_TEAM)  // derived display name rides along
  assert.equal(detectSnakeEvents(r1.next, a).events.some((e) => e.type === 'snake_status'), false)
  const b = resolveSnakeState(docFor('post-pick.html'))
  assert.ok(detectSnakeEvents(r1.next, b).events.some((e) => e.type === 'snake_status'))
})

test('snake_pick: board-delta picks → player, team (column header), is_yours, global pick #', () => {
  const st = resolveSnakeState(docFor('board-mid.html'))
  const picks = detectSnakeEvents(initSnakeMemory(), st).events.filter((e) => e.type === 'snake_pick')
  assert.ok(picks.length >= 40)
  const first = picks.find((p) => p.payload.pick_number === 1)
  assert.equal(first.payload.player_name, 'Bijan Robinson')
  assert.equal(first.payload.nfl_team, 'ATL')
  assert.equal(first.payload.position, 'RB')
  assert.equal(first.payload.picker, 'Team 1')
  assert.equal(first.payload.round, 1)
  // my own picks are tagged is_yours
  const mine = picks.filter((p) => p.payload.is_yours)
  assert.ok(mine.length >= 1)
  assert.ok(mine.every((p) => p.payload.picker === MY_TEAM))
  // deduped by board cell on the next tick
  const { next } = detectSnakeEvents(initSnakeMemory(), st)
  assert.equal(detectSnakeEvents(next, st).events.filter((e) => e.type === 'snake_pick').length, 0)
})

test('snake_pick global number reverses correctly across the round boundary', () => {
  // Round 2 pick 1 ("2.1") in a 12-team league is global pick 13.
  const st = resolveSnakeState(docFor('board-mid.html'))
  const picks = detectSnakeEvents(initSnakeMemory(), st).events.filter((e) => e.type === 'snake_pick')
  const r2p1 = picks.find((p) => p.payload.round === 2 && p.payload.pick_number === 13)
  assert.ok(r2p1, 'round-2 pick 1 maps to global pick 13')
})


// ---------------------------------------------------------------------------
// NO DRAFT BOARD IN THE PAGE (#461)
//
// The resolver used to take the viewer's own team ONLY from the board grid's
// `.myTeam` header, and asserted in its own comment that "runtime always has the
// board". That was false and untested — the three captures that would have
// covered it are truncated mid-tag at 4807 bytes, and the only complete capture
// is from a MOCK draft.
//
// A customer in a REAL league draft had no board grid. The consequences follow
// mechanically from the dependency: own-team unresolvable, so isYourTurn could
// never be true, so `your_turn` never fired, so the AI recommendation never ran
// for the entire draft — while the clock and pick train kept `snake_status`
// flowing, so the round and pick numbers displayed correctly the whole time.
// Their words: "correctly indicating what round # and pick # it was but nothing
// else", and "never populated recommended picks at all".
//
// These fixtures are the real captures with ONLY the board grid removed.
// ---------------------------------------------------------------------------
function docWithoutBoard(name) {
  const doc = docFor(name)
  for (const el of Array.from(
    doc.querySelectorAll('.draft-board-grid-header-cell, .completedPick')
  )) {
    el.remove()
  }
  return doc
}

test('no board: YOUR TURN is still detected, so the recommendation still runs', () => {
  // THE REGRESSION THAT COST A CUSTOMER A DRAFT. Without this, your_turn is 0.
  const doc = docWithoutBoard('on-the-clock.html')
  assert.equal(resolveMyTeam(doc), null, 'precondition: no board to read')

  const st = resolveSnakeState(doc)
  assert.equal(st.isYourTurn, true)
  assert.equal(st.picksUntil, 0)

  const events = detectSnakeEvents(initSnakeMemory(), st).events
  assert.equal(events.filter((e) => e.type === 'your_turn').length, 1)
})

test('no board: the countdown to your next turn still works', () => {
  const st = resolveSnakeState(docWithoutBoard('your-turn-soon.html'))
  assert.equal(st.isYourTurn, false)
  assert.equal(st.picksUntil, 2)   // from ESPN's own-pick marker, not the board
})

test('no board: the round and pick numbers keep working', () => {
  // This is the part the customer SAW working, and it must not regress — it is
  // what proves the reader was alive while everything else was dead.
  const st = resolveSnakeState(docWithoutBoard('board-mid.html'))
  assert.equal(st.round, 4)
  assert.equal(st.currentPick, 46)
  const events = detectSnakeEvents(initSnakeMemory(), st).events
  assert.ok(events.some((e) => e.type === 'snake_status'))
})

test('no board: the own-team display name still resolves, from the pick train', () => {
  const st = resolveSnakeState(docWithoutBoard('board-mid.html'))
  assert.equal(st.myTeam, MY_TEAM)
  assert.equal(st.picksUntil, 13)
})

test('no board: completed picks are absent, and that is expected', () => {
  // The board is where ESPN renders completed picks, so this cannot be recovered
  // from elsewhere. It must DEGRADE, not disable turn detection with it.
  const st = resolveSnakeState(docWithoutBoard('board-mid.html'))
  assert.equal(st.completedPicks.length, 0)
  assert.equal(st.isYourTurn, false)          // still computed
  assert.equal(st.picksUntil, 13)             // still computed
})

test('own-turn detection does not rely on comparing team names', () => {
  // Name comparison was the original mechanism and it needed the board to supply
  // one side of the comparison. Blank both name sources and the marker must still
  // carry it.
  const doc = docWithoutBoard('on-the-clock.html')
  const cp = doc.querySelector('[data-testid="current-pick"]')
  cp.removeAttribute('title')
  for (const el of Array.from(cp.querySelectorAll('.team-name'))) el.remove()

  const st = resolveSnakeState(doc)
  assert.equal(st.isYourTurn, true, 'the own-pick marker alone must be enough')
})
