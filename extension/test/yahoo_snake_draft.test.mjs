import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import {
  parseSnakeState,
  detectSnakeEvents,
  parsePickCards,
  findPicksButton,
} from '../src/content_scripts/yahoo_snake_draft_observer.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const manifest = JSON.parse(
  readFileSync(join(__dirname, '..', 'manifest.json'), 'utf-8')
)
const snakeSrc = readFileSync(
  join(__dirname, '..', 'src', 'content_scripts', 'yahoo_snake_draft.js'),
  'utf-8'
)

// Representative #app innerText snapshots from a live June 2026 snake session.
const SOMEONE_ELSE = [
  "Bart's Pick • You're up in 9 Picks • Round 7, Pick 84",
].join('\n')

const YOUR_TURN = ['YOUR TURN • ROUND 8, PICK 93'].join('\n')

// Each pick CARD's innerText (confirmed live): pick# / team / player / position
// / nfl / "Bye N"(optional). parsePickCards takes the array of card texts the
// content script collects via querySelectorAll.
const PICK_CARDS = [
  '3\nNick\nJ. Cook III\nRB\nBuf\nBye 12', // out of order on purpose
  '1\nYou\nJ. Chase\nWR\nCin\nBye 10',
  '2\nQuy\nB. Robinson\nRB\nAtl', // no Bye line
]

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

// --- parsePickCards (the pure core of getAllPicks) --------------------------

test('parsePickCards returns pick number, team, and player', () => {
  const picks = parsePickCards(PICK_CARDS)
  assert.deepEqual(picks[0], {
    pick_number: 1,
    team: 'You',
    player_name: 'J. Chase',
    position: 'WR',
    nfl_team: 'Cin',
    is_yours: true,
  })
})

test('parsePickCards marks is_yours true when the team is You', () => {
  const picks = parsePickCards(PICK_CARDS)
  assert.equal(picks.find((p) => p.pick_number === 1).is_yours, true)
  assert.equal(picks.find((p) => p.pick_number === 2).is_yours, false)
})

test('parsePickCards filters cards with an invalid position', () => {
  const cards = ['5\nQuy\nSome Name\nNOTAPOS\nAtl', '1\nYou\nJ. Chase\nWR\nCin']
  const picks = parsePickCards(cards)
  assert.equal(picks.length, 1)
  assert.equal(picks[0].pick_number, 1)
})

test('parsePickCards sorts by pick number', () => {
  // Input order is 3, 1, 2 — output must be 1, 2, 3.
  assert.deepEqual(
    parsePickCards(PICK_CARDS).map((p) => p.pick_number),
    [1, 2, 3]
  )
})

test('parsePickCards handles a missing nfl_team', () => {
  const picks = parsePickCards(['7\nNick\nX. Player\nQB'])
  assert.equal(picks.length, 1)
  assert.equal(picks[0].nfl_team, null)
})

test('parsePickCards preserves a III suffix in the player name', () => {
  const cook = parsePickCards(PICK_CARDS).find((p) => p.pick_number === 3)
  assert.equal(cook.player_name, 'J. Cook III')
})

test('parsePickCards skips cards whose first line is not a pure pick number', () => {
  // "Round 4" / stat columns must not be read as a pick number.
  assert.deepEqual(parsePickCards(['Round 4\nNick\nX. Player\nQB\nBuf']), [])
  assert.deepEqual(parsePickCards(['12.5\nNick\nX. Player\nQB\nBuf']), [])
})

test('parsePickCards returns [] on empty/short input', () => {
  assert.deepEqual(parsePickCards([]), [])
  assert.deepEqual(parsePickCards(null), [])
  assert.deepEqual(parsePickCards(['1\nYou\nJ. Chase']), []) // < 4 parts
})

// --- content-script wiring (CSS selector + MutationObserver) ----------------

test('getAllPicks queries the pick-card selector and parses via parsePickCards', () => {
  assert.match(snakeSrc, /querySelectorAll\(PICK_CARD_SELECTOR\)/)
  assert.match(snakeSrc, /parsePickCards\(/)
  // The distinctive pick-card class is part of the selector.
  assert.match(snakeSrc, /ys-colors-surface-accent/)
})

test('pollPicksPanel reads the cards (getAllPicks), not #app innerText', () => {
  assert.match(snakeSrc, /const picks = getAllPicks\(\)/)
  assert.match(snakeSrc, /for \(const pick of picks\)/)
  // The old text-based parsePicks is gone.
  assert.doesNotMatch(snakeSrc, /parsePicks\(/)
})

test('a MutationObserver watches the picks container for new cards', () => {
  assert.match(snakeSrc, /PICK_CONTAINER_SELECTOR/)
  assert.match(snakeSrc, /new MutationObserver\(\(\)\s*=>\s*\{\s*pollPicksPanel\(\)/)
  assert.match(snakeSrc, /childList: true/)
})

// --- Picks-tab auto-click (cards only render while the tab is active) -------

test('findPicksButton returns the button whose text is Picks', () => {
  const buttons = [
    { innerText: 'Available' },
    { innerText: '  Picks  ' }, // whitespace tolerated
    { innerText: 'Rosters' },
  ]
  assert.equal(findPicksButton(buttons), buttons[1])
})

test('findPicksButton returns null when no Picks button exists', () => {
  assert.equal(findPicksButton([{ innerText: 'Available' }]), null)
  assert.equal(findPicksButton([]), null)
  assert.equal(findPicksButton(null), null)
})

test('findPicksButton does not match a partial label', () => {
  // "Picks (40)" is not the tab button — only an exact "Picks" is.
  assert.equal(findPicksButton([{ innerText: 'Picks (40)' }]), null)
})

test('clickPicksTab finds the Picks button via findPicksButton and clicks it', () => {
  assert.match(snakeSrc, /findPicksButton\(Array\.from\(document\.querySelectorAll\('button'\)\)\)/)
  assert.match(snakeSrc, /btn\.click\(\)/)
})

test('init retries clicking the Picks tab until the button is found', () => {
  // startWhenPicksReady: click succeeds -> start poller; else retry in 1s.
  assert.match(snakeSrc, /function startWhenPicksReady/)
  assert.match(snakeSrc, /setTimeout\(startWhenPicksReady, 1000\)/)
  assert.match(snakeSrc, /setTimeout\(startPoller, 500\)/)
})

test('pollPicksPanel re-clicks the Picks tab when 0 cards after known picks', () => {
  assert.match(
    snakeSrc,
    /picks\.length === 0 && sentPickNumbers\.size > 0/
  )
  // The guard re-clicks and returns before trying to relay.
  assert.match(snakeSrc, /clickPicksTab\(\)\s*\n\s*return/)
})
