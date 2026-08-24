/**
 * Coarse "how long ago" label for a duration in milliseconds.
 *
 * Deliberately coarse and never rounded up: during a live draft this sits next
 * to the connection status, where an over-precise ticking number reads as noise
 * and an over-stated one ("1m" when it has been 61s) reads as a stall that is
 * not happening.
 */
export function formatElapsed(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '0s'
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  return `${Math.floor(minutes / 60)}h`
}
