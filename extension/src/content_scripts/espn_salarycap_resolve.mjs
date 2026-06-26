/**
 * ESPN SALARY-CAP (auction) resolver → nomination / bid_update / clock /
 * draft_pick / teams_update.
 *
 * Anchors (all PRIMARY — testid / hand-authored semantic classes):
 *   - Pick train: `[data-testid="draft-order-set"]` → 12 × `[data-testid="auction-pick"]`.
 *     Each: `.team-name` ("N. Name" → strip), `.cash` ($ budget), `.bid-amount`
 *     ($ live bid or "$null"), `.autopick`/`[data-testid="auto-pick"]`. A `.content`
 *     child carries `--own` (my team) / `--selecting` (the nominating team).
 *   - Nominee: `[data-testid="player-selected"]` → `.playerinfo__playername` /
 *     `.playerinfo__playerteam` / `.playerinfo__playerpos` (name-only; no id).
 *   - Bidding: `[data-testid="bidding-form"]` → `.current-amount` ("Current offer:
 *     $53") + `.manual-bid` ("Manual offer (max $117)").
 *   - High bidder: the one `auction-pick` whose `.bid-amount` == the current offer.
 *   - Sale: board `.completedPick` delta (winner = column-header team, price =
 *     `.winningPrice`, is_yours = `.myTeam` column). The transient
 *     `[data-testid="player-drafted"]--own` banner is only an is_yours hint.
 *
 * Emits the existing auction event contract verbatim so the backend/frontend stay
 * untouched. Pure DOM-structural → linkedom-testable (test/fixtures/espn/salarycap/).
 */
import {
  txt,
  num,
  cleanTeamName,
  resolveClock,
  resolveBoardHeaders,
  resolveMyTeam,
  resolveCompletedPicks,
} from './espn_shared.mjs'

/** Gate: salary cap is active when the auction pick train (or bid form) is up. */
export function isSalaryCap(root) {
  if (!root) return false
  return (
    root.querySelectorAll('[data-testid="auction-pick"]').length >= 1 ||
    !!root.querySelector('[data-testid="bidding-form"]')
  )
}

/** 12 team cards → { teams, myTeam, selectingTeam }. */
export function resolveTeams(root) {
  const cards = root ? Array.from(root.querySelectorAll('[data-testid="auction-pick"]')) : []
  const teams = {}
  let myTeam = null
  let selectingTeam = null
  for (const c of cards) {
    const name = cleanTeamName(txt(c.querySelector('.team-name'))) || c.getAttribute('title')
    if (!name) continue
    const isMine = !!c.querySelector('.auction-pick-component--own')
    const selecting = !!c.querySelector('.auction-pick-component--selecting')
    const bidTxt = txt(c.querySelector('.bid-amount'))
    teams[name] = {
      budget: num(txt(c.querySelector('.cash'))),
      bid: /\$\s*null/i.test(bidTxt) ? null : num(bidTxt),
      autopick: !!c.querySelector('[data-testid="auto-pick"]') || !!c.querySelector('.autopick'),
      isMine,
    }
    if (isMine) myTeam = name
    if (selecting) selectingTeam = name
  }
  return { teams, myTeam, selectingTeam }
}

/** Nominee on the block, or null when none is up. */
export function resolveNominee(root) {
  const ps = root && root.querySelector('[data-testid="player-selected"]')
  if (!ps) return null
  const name = txt(ps.querySelector('.playerinfo__playername'))
  if (!name) return null
  return {
    name,
    proTeam: txt(ps.querySelector('.playerinfo__playerteam')) || null,
    position: txt(ps.querySelector('.playerinfo__playerpos')) || null,
  }
}

/** Bidding form → { currentBid, maxBid }, or null. */
export function resolveBidding(root) {
  const bf = root && root.querySelector('[data-testid="bidding-form"]')
  if (!bf) return null
  return {
    currentBid: num(txt(bf.querySelector('.current-amount'))), // "Current offer: $53"
    maxBid: num(txt(bf.querySelector('.manual-bid'))), // "Manual offer (max $117)"
  }
}

/** Full salary-cap board state. */
export function resolveSalaryCapState(root) {
  const { teams, myTeam, selectingTeam } = resolveTeams(root)
  const nominee = resolveNominee(root)
  const bidding = resolveBidding(root)
  const headers = resolveBoardHeaders(root)
  const completedPicks = resolveCompletedPicks(root, headers)
  const currentBid = bidding ? bidding.currentBid : null
  // High bidder = the team whose live bid equals the current offer.
  let highBidder = null
  if (currentBid != null) {
    for (const [name, t] of Object.entries(teams)) {
      if (t.bid != null && t.bid === currentBid) { highBidder = name; break }
    }
  }
  return {
    active: isSalaryCap(root),
    clock: resolveClock(root),
    teams,
    myTeam: myTeam || resolveMyTeam(root),
    selectingTeam,
    nominee,
    currentBid,
    maxBid: bidding ? bidding.maxBid : null,
    highBidder,
    headers,
    completedPicks,
  }
}

// ---------------------------------------------------------------------------
// Event diffing — emits the existing auction contract. ON CHANGE only.
// ---------------------------------------------------------------------------
export function initSalaryCapMemory() {
  return {
    lastNominee: null,
    lastBid: null,
    lastBidder: null,
    lastClock: null,
    prevTeams: {},
    soldKeys: [], // dedupe draft_pick per board cell
  }
}

const posTeamStr = (n) =>
  n && (n.position || n.proTeam) ? [n.position, n.proTeam].filter(Boolean).join(' · ') : null

export function detectSalaryCapEvents(prev, curr) {
  const events = []
  const next = { ...prev, soldKeys: prev.soldKeys.slice() }
  const nom = curr.nominee
  const nomName = nom ? nom.name : null

  // SALE — every NEW completed board cell (winner = column-header team). Board-
  // delta driven (the player-drafted banner is transient); deduped per cell.
  for (const p of curr.completedPicks) {
    if (!p.name) continue
    const key = `${p.name}@${p.row}/${p.col}`
    if (next.soldKeys.includes(key)) continue
    next.soldKeys.push(key)
    // Skip the very first observation (initial board load) only if we've never
    // seen teams — otherwise relay so the UI removes the player.
    events.push({
      type: 'draft_pick',
      platform: 'espn',
      payload: {
        player_name: p.name,
        player_id: '',
        espn_player_id: null,
        pro_team: p.proTeam,
        position: p.position,
        final_price: p.winningPrice,
        winner: p.team,
        is_yours: p.isMine,
        teams_snapshot: curr.teams,
      },
    })
  }

  // NOMINATION — a new player on the block.
  if (nomName && nomName !== prev.lastNominee) {
    events.push({
      type: 'nomination',
      platform: 'espn',
      payload: {
        player_name: nomName,
        player_id: '',
        espn_player_id: null,
        pos_team: posTeamStr(nom),
        pro_team: nom.proTeam,
        position: nom.position,
        opening_bid: curr.currentBid,
        current_bidder: curr.highBidder,
        current_bidder_team_id: null,
        clock: curr.clock.digits || null,
      },
    })
    next.lastNominee = nomName
    next.lastBid = curr.currentBid
    next.lastBidder = curr.highBidder
  } else if (nomName && nomName === prev.lastNominee &&
             (curr.currentBid !== prev.lastBid || curr.highBidder !== prev.lastBidder)) {
    // BID UPDATE — same nominee, amount or high bidder changed.
    events.push({
      type: 'bid_update',
      platform: 'espn',
      payload: {
        player_name: nomName,
        current_bid: curr.currentBid,
        current_bidder: curr.highBidder,
        current_bidder_team_id: null,
        clock: curr.clock.digits || null,
      },
    })
    next.lastBid = curr.currentBid
    next.lastBidder = curr.highBidder
  }
  if (!nomName) next.lastNominee = null

  // CLOCK — while a nominee is up; 5s cadence to keep the UI ticking sans spam.
  if (nomName && curr.clock.digits && curr.clock.digits !== prev.lastClock) {
    next.lastClock = curr.clock.digits
    if (curr.clock.seconds != null && curr.clock.seconds % 5 === 0) {
      events.push({
        type: 'clock',
        platform: 'espn',
        payload: {
          player_name: nomName,
          clock: curr.clock.digits,
          seconds_remaining: curr.clock.seconds,
        },
      })
    }
  }

  // TEAMS UPDATE — budgets/bids/autopick changed.
  if (JSON.stringify(curr.teams) !== JSON.stringify(prev.prevTeams)) {
    events.push({
      type: 'teams_update',
      platform: 'espn',
      payload: { teams: curr.teams, your_team_id: curr.myTeam },
    })
    next.prevTeams = { ...curr.teams }
  }

  return { events, next }
}
