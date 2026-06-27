/**
 * Sleeper AUCTION resolver → nomination / bid_update / draft_pick / teams_update.
 *
 * Auction frames on `draft:<id>`:
 *   - `draft_updated_by_nomination` — metadata{nominated_player_id, nominating_slot,
 *     highest_offer (opening), timer_end_at}. **Player-id-only — no name** (the
 *     backend resolves sleeper_id → player; that's the additive lookup this needs).
 *   - `new_draft_offer` — {slot, amount, player_id} (a bid). `draft_updated_by_offer`
 *     refreshes metadata{offering_slot, highest_offer, timer_end_at}.
 *   - `player_picked` — the SALE, now with metadata{slot, amount, name…}.
 *
 * Budgets aren't pushed per-team, so teams_update is derived (budget − Σ won).
 * Clock rides as an absolute `clock_ends_at` (ISO `timer_end_at`); the content
 * script converts to seconds_remaining at post time (keeps this pure/testable).
 */
import { isDraftFrame, readConfig, mySlotFrom, teamLabel } from './sleeper_resolve.mjs'

export function initAuctionMemory(myUserId) {
  return {
    myUserId: myUserId != null ? String(myUserId) : null,
    teams: null,
    budget: null,
    draftOrder: null,
    mySlot: null,
    currentNominee: null, // sleeper_player_id on the block
    lastBidKey: null, // dedupe bid_update
    timerEndsAt: null,
    soldKeys: [], // dedupe draft_pick by pick_no
    spentBySlot: {}, // derived budgets
    prevTeamsKey: null,
  }
}

function applyConfig(next, payload) {
  const c = readConfig(payload)
  if (c.teams != null) next.teams = c.teams
  if (c.budget != null) next.budget = c.budget
  if (c.draftOrder) next.draftOrder = c.draftOrder
  const ms = mySlotFrom(next.draftOrder, next.myUserId)
  if (ms != null) next.mySlot = ms
  const md = payload.metadata || {}
  if (md.timer_end_at) next.timerEndsAt = md.timer_end_at
}

function teamsSnapshot(next) {
  const teams = {}
  if (!next.teams) return teams
  for (let slot = 1; slot <= next.teams; slot++) {
    teams[teamLabel(slot)] = {
      budget: next.budget != null ? next.budget - (next.spentBySlot[slot] || 0) : null,
      isMine: slot === next.mySlot,
    }
  }
  return teams
}

export function detectAuctionEvents(memory, frame) {
  const events = []
  const next = { ...memory, soldKeys: memory.soldKeys.slice(), spentBySlot: { ...memory.spentBySlot } }
  if (!isDraftFrame(frame)) return { events, next }
  const p = frame.payload || {}
  if (p.settings || p.draft_order || p.metadata) applyConfig(next, p)
  const md = p.metadata || {}

  // NOMINATION — a new player on the block (id-only).
  if (frame.event === 'draft_updated_by_nomination') {
    const nominee = md.nominated_player_id || null
    if (nominee && nominee !== next.currentNominee) {
      next.currentNominee = nominee
      next.lastBidKey = null
      events.push({
        type: 'nomination',
        platform: 'sleeper',
        payload: {
          player_name: null, // resolved backend-side from sleeper_player_id
          player_id: '',
          sleeper_player_id: nominee,
          pos_team: null,
          opening_bid: md.highest_offer != null ? Number(md.highest_offer) : null,
          current_bidder: teamLabel(md.offering_slot ? Number(md.offering_slot) : Number(md.nominating_slot)),
          current_bidder_team_id: null,
          nominating_slot: md.nominating_slot != null ? Number(md.nominating_slot) : null,
          clock_ends_at: md.timer_end_at || next.timerEndsAt || null,
        },
      })
    }
  }

  // BID — a new offer on the current nominee.
  if (frame.event === 'new_draft_offer') {
    const slot = p.slot != null ? Number(p.slot) : null
    const amount = p.amount != null ? Number(p.amount) : null
    const key = `${p.player_id}:${amount}:${slot}`
    if (amount != null && key !== next.lastBidKey) {
      next.lastBidKey = key
      next.currentNominee = p.player_id || next.currentNominee
      events.push({
        type: 'bid_update',
        platform: 'sleeper',
        payload: {
          player_name: null,
          sleeper_player_id: p.player_id || null,
          current_bid: amount,
          current_bidder: teamLabel(slot),
          current_bidder_team_id: null,
          clock_ends_at: next.timerEndsAt || null,
        },
      })
    }
  }

  // SALE — the nominee was won.
  if (frame.event === 'player_picked') {
    const pickNo = p.pick_no
    const slot = md.slot != null ? Number(md.slot) : null
    const price = md.amount != null ? Number(md.amount) : null
    if (pickNo != null && !next.soldKeys.includes(pickNo)) {
      next.soldKeys.push(pickNo)
      if (slot != null && price != null) next.spentBySlot[slot] = (next.spentBySlot[slot] || 0) + price
      next.currentNominee = null
      next.lastBidKey = null
      events.push({
        type: 'draft_pick',
        platform: 'sleeper',
        payload: {
          player_name: `${md.first_name || ''} ${md.last_name || ''}`.trim() || null,
          player_id: '',
          sleeper_player_id: p.player_id || null,
          position: md.position || null,
          pro_team: md.team || null,
          final_price: price,
          winner: teamLabel(slot),
          is_yours: slot != null && slot === next.mySlot,
          teams_snapshot: teamsSnapshot(next),
        },
      })
      // TEAMS UPDATE — budgets shifted.
      const teams = teamsSnapshot(next)
      const key = JSON.stringify(teams)
      if (key !== next.prevTeamsKey) {
        next.prevTeamsKey = key
        events.push({
          type: 'teams_update',
          platform: 'sleeper',
          payload: { teams, your_team_id: teamLabel(next.mySlot) },
        })
      }
    }
  }

  return { events, next }
}
