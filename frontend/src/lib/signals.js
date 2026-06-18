/**
 * Derive the effective display signal for a player.
 *
 * Mirrors derive_system_signal() in backend/engines/backtest.py.
 * Used to prevent misleading "avoid" badges on cheap players or
 * players within auction-noise gaps.
 */

import { getBidCeiling } from '../utils/playerUtils'

const BUY_ASSESSMENTS = ['elite_value', 'good_value']

export function getDisplaySignal(player) {
  const price = player.market_value_league || 0
  const gap = (getBidCeiling(player) || 0) - price
  const assessment = player.value_assessment
  const payUp = player.pay_up_flag

  if (payUp) return 'strong_buy'

  // Cheap players — never show avoid
  if (price <= 8) {
    if (BUY_ASSESSMENTS.includes(assessment)) return 'strong_buy'
    return 'neutral'
  }

  // Small gap — neutral not avoid
  if (gap >= -8 && gap <= 0) {
    if (BUY_ASSESSMENTS.includes(assessment)) return 'buy'
    return 'neutral'
  }

  // Normal logic for everything else
  if (BUY_ASSESSMENTS.includes(assessment)) {
    return gap >= 5 ? 'strong_buy' : 'buy'
  }
  if (['avoid', 'slight_overpay'].includes(assessment) && gap <= -8) {
    return gap <= -15 ? 'strong_avoid' : 'avoid'
  }

  return 'neutral'
}

/**
 * Map a display signal to badge styling.
 */
export function getSignalBadgeStyle(signal) {
  switch (signal) {
    case 'strong_buy':
    case 'buy':
      return 'bg-emerald-500/15 text-emerald-400'
    case 'avoid':
    case 'strong_avoid':
      return 'bg-red-500/15 text-red-400'
    default:
      return 'bg-slate-500/15 text-slate-400'
  }
}

/**
 * Map a display signal to human-readable label.
 */
export function getSignalLabel(signal) {
  switch (signal) {
    case 'strong_buy': return 'strong buy'
    case 'buy': return 'buy'
    case 'avoid': return 'avoid'
    case 'strong_avoid': return 'strong avoid'
    default: return 'neutral'
  }
}
