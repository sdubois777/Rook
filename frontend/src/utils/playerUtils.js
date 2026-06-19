/**
 * Player display utilities — the SINGLE source of truth for how player ADP /
 * valuation fields are selected and formatted.
 *
 * Import from here; never read raw player ADP fields directly in components.
 * The recurring bug this prevents: adp_ai is the raw model estimate (a float
 * with heavy ties — Bijan/Gibbs/Chase all 4.0), while adp_rank is the clean
 * 1..N integer the board actually shows. Components kept reaching for adp_ai
 * and displaying inconsistent values. getDisplayAdp() always uses adp_rank.
 *
 * Two object shapes flow through the app:
 *   - PLAYER ROWS  (draftboard / available list): adp_rank, adp_fantasypros,
 *     adp_diff, ai_bid_ceiling, snake_flag
 *   - RECOMMENDATIONS (engine output): adp_rank|adp_ai, adp_fp, adp_diff
 *     (note: adp_fp, NOT adp_fantasypros). The getRec* helpers cover this shape.
 */
import { normalizeName } from './names'

// --- Player-row ADP selection (never adp_ai) --------------------------------

/** Display ADP — always the clean adp_rank integer, never the tied adp_ai. */
export function getDisplayAdp(player) {
  return player?.adp_rank ?? null
}

/** FantasyPros consensus ADP (also a rank, integer-valued). */
export function getFpAdp(player) {
  return player?.adp_fantasypros ?? null
}

/** ADP differential (fp_rank − adp_rank). Positive = we rate them earlier. */
export function getAdpDiff(player) {
  return player?.adp_diff ?? null
}

/** Auction bid ceiling. */
export function getBidCeiling(player) {
  return player?.ai_bid_ceiling ?? null
}

/** The mode-appropriate primary value: adp_rank (snake) or ceiling (auction). */
export function getPrimaryValue(player, isSnake) {
  return isSnake ? getDisplayAdp(player) : getBidCeiling(player)
}

// --- Player-row formatting --------------------------------------------------

/** Clean integer string for the display ADP, or '--'. */
export function formatAdp(player) {
  const adp = getDisplayAdp(player)
  return adp != null ? String(adp) : '--'
}

/** FP ADP as an integer string (it's a rank), or '--'. */
export function formatFpAdp(player) {
  const fp = getFpAdp(player)
  return fp != null ? String(Math.round(fp)) : '--'
}

/** Signed integer string for the ADP diff: '+3', '-7', '0', or '--'. */
export function formatAdpDiff(player) {
  const diff = getAdpDiff(player)
  if (diff == null) return '--'
  const rounded = Math.round(diff)
  return rounded > 0 ? `+${rounded}` : String(rounded)
}

// --- Sort comparators -------------------------------------------------------

/** Snake draft order: adp_rank ascending; missing ranks sort last. */
export function snakeSortComparator(a, b) {
  return (a?.adp_rank ?? 9999) - (b?.adp_rank ?? 9999)
}

/** Auction draft order: ai_bid_ceiling descending; missing ceilings last. */
export function auctionSortComparator(a, b) {
  return (b?.ai_bid_ceiling ?? 0) - (a?.ai_bid_ceiling ?? 0)
}

// --- Snake suggested targets (urgency-aware) --------------------------------

// Starter lineup this league fields (bench excluded). FLEX accepts RB/WR/TE.
export const STARTER_SLOTS = { QB: 1, RB: 2, WR: 2, TE: 1, FLEX: 1, K: 1, DEF: 1 }

/** Starter positions a roster still needs. FLEX opens RB/WR/TE. */
export function neededPositions(myRoster) {
  const filled = {}
  for (const p of myRoster || []) {
    const pos = p.position || 'BN'
    filled[pos] = (filled[pos] || 0) + 1
  }
  const needed = new Set()
  for (const [pos, count] of Object.entries(STARTER_SLOTS)) {
    if ((filled[pos] || 0) < count) needed.add(pos)
  }
  if ((filled.FLEX || 0) < 1) {
    needed.add('RB')
    needed.add('WR')
    needed.add('TE')
  }
  return needed
}

/**
 * How many picks until a player is likely gone, given the CURRENT pick and team
 * count: roughly fp consensus rank minus the current overall pick.
 */
export function picksUntilGone(player, currentPick) {
  return (getFpAdp(player) ?? 999) - (currentPick || 1)
}

/**
 * Urgency score for a snake target: higher = draft sooner. A player likely gone
 * before your next turn is urgent; a high adp_diff (market is sleeping on them,
 * so they'll last) lowers urgency; tier-1 players are always urgent.
 */
export function snakeUrgencyScore(player, currentPick, totalTeams = 12) {
  const teams = totalTeams || 12
  const untilGone = picksUntilGone(player, currentPick)
  const adpDiff = getAdpDiff(player) ?? 0
  const adpRank = getDisplayAdp(player) ?? 999

  let score = 0
  if (untilGone <= teams) score += 100 // gone before your next pick
  else if (untilGone <= teams * 2) score += 50 // gone within two turns

  if (adpDiff > 20) score -= 30 // market sleeping — you can wait
  else if (adpDiff > 10) score -= 15

  if (adpRank <= 12) score += 50 // tier-1 always urgent
  return score
}

/**
 * Suggested snake targets: needed-position players ranked by urgency (will they
 * be gone before your next pick?), tie-broken by adp_rank. Falls back to best
 * available when every starter slot is filled. Top 8.
 */
export function getSnakeTargets(myRoster, availablePlayers, currentPick, totalTeams = 12) {
  if (!availablePlayers?.length) return []

  const needed = neededPositions(myRoster)
  let candidates = availablePlayers.filter((p) => needed.has(p.position))
  if (candidates.length === 0) candidates = [...availablePlayers] // starters full

  return candidates
    .map((p) => ({
      ...p,
      _urgency: snakeUrgencyScore(p, currentPick, totalTeams),
      _picksUntilGone: picksUntilGone(p, currentPick),
    }))
    .sort((a, b) =>
      b._urgency !== a._urgency
        ? b._urgency - a._urgency
        : (getDisplayAdp(a) ?? 999) - (getDisplayAdp(b) ?? 999)
    )
    .slice(0, 8)
}

/**
 * The urgency badge for a target row: Now (gone within a round), Soon (within
 * two), Wait (market sleeping — high adp_diff), or null.
 */
export function snakeUrgencyLabel(target, totalTeams = 12) {
  const teams = totalTeams || 12
  const untilGone = target?._picksUntilGone ?? picksUntilGone(target, 1)
  if (untilGone <= teams) return { text: 'Now', className: 'text-red-400' }
  if (untilGone <= teams * 2) return { text: 'Soon', className: 'text-amber-400' }
  if ((getAdpDiff(target) ?? 0) > 10) return { text: 'Wait', className: 'text-emerald-400' }
  return null
}

// --- Snake flag badge -------------------------------------------------------

const SNAKE_FLAG_CONFIG = {
  VALUE: { label: 'VALUE', color: 'green', className: 'text-emerald-400 bg-emerald-500/15' },
  SLEEPER: { label: 'SLEEPER', color: 'purple', className: 'text-purple-400 bg-purple-500/15' },
  TARGET: { label: 'TARGET', color: 'blue', className: 'text-blue-400 bg-blue-500/15' },
  REACH: { label: 'REACH', color: 'orange', className: 'text-orange-400 bg-orange-500/15' },
}

/** Flag badge config { label, color } — defaults to TARGET for unknown/missing. */
export function getSnakeFlag(player) {
  return SNAKE_FLAG_CONFIG[player?.snake_flag] ?? SNAKE_FLAG_CONFIG.TARGET
}

/** Tailwind classes for a player's snake flag badge (TARGET-styled fallback). */
export function getSnakeFlagClass(player) {
  return (SNAKE_FLAG_CONFIG[player?.snake_flag] ?? SNAKE_FLAG_CONFIG.TARGET).className
}

/** The raw flag label, or null when the player has none (for badge rendering). */
export function getSnakeFlagLabel(player) {
  return player?.snake_flag ?? null
}

// --- Name matching (snake DOM names are abbreviated) ------------------------

/**
 * Does a player's full name match a (possibly abbreviated) pick name?
 *
 * The Yahoo snake DOM gives abbreviated names ("C. MCCAFFREY", "G. PICKENS"),
 * which won't equal the full names in the available list. Match strategy:
 *   1. exact normalized equality
 *   2. abbreviated first name: same last name + same first initial
 *      ("c mccaffrey" == "christian mccaffrey")
 *
 * Centralized here so every consumer (recordSnakePick, etc.) matches the same
 * way — the backend resolves abbreviations too, this is the frontend backstop.
 */
export function matchesPickName(playerName, pickName) {
  const a = normalizeName(playerName)
  const b = normalizeName(pickName)
  if (!a || !b) return false
  if (a === b) return true

  const ap = a.split(' ')
  const bp = b.split(' ')
  if (ap.length >= 2 && bp.length >= 2) {
    const aLast = ap.slice(1).join(' ')
    const bLast = bp.slice(1).join(' ')
    if (aLast === bLast && ap[0][0] === bp[0][0]) return true
  }
  return false
}

// --- Recommendation object (engine output — a DIFFERENT shape) --------------
// Recs may carry adp_rank (your_turn path) or legacy adp_ai (nomination path),
// and use adp_fp (not adp_fantasypros). Centralize that selection here too so
// the recommendation card stays consistent with the board.

/** Rec display ADP — prefer the clean adp_rank, fall back to rounded adp_ai. */
export function getRecAdp(rec) {
  if (rec?.adp_rank != null) return rec.adp_rank
  if (rec?.adp_ai != null) return Math.round(rec.adp_ai)
  return null
}

/** Rec FP ADP (engine field is adp_fp). */
export function getRecFpAdp(rec) {
  return rec?.adp_fp ?? null
}

/** Rec ADP diff. */
export function getRecAdpDiff(rec) {
  return rec?.adp_diff ?? null
}
