/**
 * The single shared player name + badges primitive. Every surface that shows a
 * player renders through this (or its `PlayerBadges` half), so the position badge,
 * the injury badge, and any future tag are wired ONCE — not per page.
 *
 * Two exports:
 *   <PlayerBadges position injuryStatus variant /> — the atomic badge cluster
 *     (position + injury), a drop-in replacement for a bare <PositionBadge>. Use
 *     this on surfaces whose name markup is bespoke (trend icons, BUY/SELL tags).
 *   <PlayerName name position injuryStatus variant /> — badges + the name, for the
 *     common "badge then name" row.
 *
 * `variant` controls DENSITY only (recon area 6 — preserve existing sizes):
 *   'dense'   (default) — bordered text-xs, the shared-surface look (pixel-intact).
 *   'compact'          — borderless text-[10px], the Trade/Waiver row look.
 * Both variants use the SHARED position palette. Injury status null → no injury badge.
 * Display-only — no value/metered path.
 */
import PositionBadge from './PositionBadge'
import InjuryBadge from './InjuryBadge'

export function PlayerBadges({ position, injuryStatus, variant = 'dense', className = '' }) {
  return (
    <span className={`inline-flex shrink-0 items-center gap-1 ${className}`}>
      <PositionBadge position={position} variant={variant} />
      <InjuryBadge status={injuryStatus} variant={variant} />
    </span>
  )
}

export default function PlayerName({
  name,
  position,
  injuryStatus,
  variant = 'dense',
  className = '',
  nameClassName = 'text-sm font-medium text-white',
  children,
}) {
  return (
    <span className={`inline-flex min-w-0 items-center gap-1.5 ${className}`}>
      <PlayerBadges position={position} injuryStatus={injuryStatus} variant={variant} />
      <span className={`truncate ${nameClassName}`}>{name}</span>
      {children}
    </span>
  )
}
