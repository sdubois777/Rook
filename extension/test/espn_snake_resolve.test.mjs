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
  const st = resolveSnakeState(docFor('on-the-clock.html'), { myTeam: MY_TEAM })
  assert.equal(st.currentPick, 11)
  assert.equal(st.onClockTeam, MY_TEAM)
  assert.equal(st.isYourTurn, true)
  assert.equal(st.round, 1)
})

test('status: opponent on the clock, my next pick is 2 away', () => {
  const st = resolveSnakeState(docFor('your-turn-soon.html'), { myTeam: MY_TEAM })
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
  const st = resolveSnakeState(docFor('on-the-clock.html'), { myTeam: MY_TEAM })
  const first = detectSnakeEvents(initSnakeMemory(), st)
  const yt = first.events.find((e) => e.type === 'your_turn')
  assert.ok(yt)
  assert.deepEqual(yt.payload, { round: 1, pick: 11, picks_until_your_turn: 0 })
  assert.equal(detectSnakeEvents(first.next, st).events.some((e) => e.type === 'your_turn'), false)
})

test('your_turn_soon fires once at exactly 2 away', () => {
  const st = resolveSnakeState(docFor('your-turn-soon.html'), { myTeam: MY_TEAM })
  const start = { ...initSnakeMemory(), lastPicksUntil: 3 }
  const r = detectSnakeEvents(start, st)
  const soon = r.events.find((e) => e.type === 'your_turn_soon')
  assert.ok(soon)
  assert.equal(soon.payload.picks_until_your_turn, 2)
  assert.equal(detectSnakeEvents(r.next, st).events.some((e) => e.type === 'your_turn_soon'), false)
})

test('snake_status carries pick/round/countdown, deduped then re-fires on change', () => {
  const a = resolveSnakeState(docFor('your-turn-soon.html'), { myTeam: MY_TEAM })
  const r1 = detectSnakeEvents(initSnakeMemory(), a)
  const s1 = r1.events.find((e) => e.type === 'snake_status')
  assert.equal(s1.payload.current_pick, 9)
  assert.equal(s1.payload.current_round, 1)
  assert.equal(s1.payload.picks_until_your_turn, 2)
  assert.equal(detectSnakeEvents(r1.next, a).events.some((e) => e.type === 'snake_status'), false)
  const b = resolveSnakeState(docFor('post-pick.html'), { myTeam: MY_TEAM })
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
