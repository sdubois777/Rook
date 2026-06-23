import { useState, useEffect, useRef } from 'react'
import { useDraftStore } from '../../stores/draft'
import PositionBadge from '../shared/PositionBadge'

/** "0:15" from a seconds count. */
function fmtClock(secs) {
  if (secs == null) return null
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

// Where the live bid sits relative to our own system value.
function valueIndicator(bid, systemValue) {
  if (bid == null || systemValue == null) return null
  const gap = bid - systemValue
  if (gap <= -3) return { label: 'Undervalued', cls: 'text-emerald-400' }
  if (gap >= 3) return { label: 'Overvalued', cls: 'text-red-400' }
  return { label: 'Aligned', cls: 'text-slate-400' }
}

export default function NominationPanel() {
  const rec = useDraftStore((s) => s.recommendation)
  const currentBid = useDraftStore((s) => s.currentBid)
  const currentNomination = useDraftStore((s) => s.currentNomination)

  const nom = currentNomination
  const playerName =
    nom?.playerName || rec?.player_name || currentBid?.player_name || 'Unknown'
  const bidAmount = nom?.currentBid ?? currentBid?.current_bid

  // Local 1s countdown so the timer ticks SMOOTHLY and never appears frozen
  // between the extension's coarse (5s) clock updates. It resyncs to the
  // extension's value on every fresh clock/bid tick; between them it counts down
  // locally. (Hooks run unconditionally — before the early return below.)
  const targetSecs = nom?.secondsRemaining ?? null
  const [secs, setSecs] = useState(targetSecs)
  useEffect(() => setSecs(targetSecs), [targetSecs])

  // Anti-snipe: Yahoo bumps the clock back up to 10s when a HIGHER bid lands in
  // the final seconds. Our scraped clock doesn't reflect that reset, so mirror
  // it locally — on a higher bid, raise the countdown to 10s if it's below.
  // Declared AFTER the resync above so it wins over any stale clock value
  // delivered alongside the same bid.
  const prevBidRef = useRef(null)
  useEffect(() => {
    const prev = prevBidRef.current
    prevBidRef.current = bidAmount
    if (bidAmount != null && prev != null && bidAmount > prev) {
      setSecs((s) => (s != null && s < 10 ? 10 : s))
    }
  }, [bidAmount])

  useEffect(() => {
    const t = setInterval(
      () => setSecs((s) => (s != null && s > 0 ? s - 1 : s)),
      1000
    )
    return () => clearInterval(t)
  }, [])

  const clock = nom ? fmtClock(secs) : null
  const clockDanger = secs != null && secs < 10
  const hasNomination = nom || rec || currentBid

  const systemValue = rec?.system_value ?? null
  const marketValue = rec?.market_value ?? null
  const indicator = valueIndicator(bidAmount, systemValue)

  if (!hasNomination) {
    return (
      <div className="h-full flex flex-col p-3">
        <h3 className="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">
          Current Nomination
        </h3>
        <div className="flex-1 flex items-center justify-center text-slate-600">
          <p>Waiting for nomination...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col p-3">
      <h3 className="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">
        Current Nomination
      </h3>

      {/* Nominee */}
      <div className="flex items-center gap-2 mb-2">
        <PositionBadge position={rec?.position} />
        <span className="text-lg font-bold text-slate-100 truncate">{playerName}</span>
        {nom?.posTeam && <span className="text-xs text-slate-500">{nom.posTeam}</span>}
      </div>

      {/* Current bid + clock */}
      <div className="bg-surface-2 rounded-lg px-3 py-2 mb-2 flex items-center justify-between">
        <div>
          <div className="text-xs text-slate-500 mb-0.5">Current Bid</div>
          <div className="text-2xl font-mono font-bold text-amber-400">
            ${bidAmount ?? '--'}
          </div>
        </div>
        {clock && (
          <div className="text-right">
            <div className="text-xs text-slate-500 mb-0.5">Remaining</div>
            <div
              className={`text-2xl font-mono font-bold ${
                clockDanger ? 'text-red-500' : 'text-slate-300'
              }`}
            >
              {clock}
            </div>
          </div>
        )}
      </div>

      {/* System / Market + value indicator */}
      {(systemValue != null || marketValue != null) && (
        <div className="flex items-center gap-4 text-sm">
          <span className="text-slate-500">
            System: <span className="font-mono text-slate-300">${systemValue ?? '--'}</span>
          </span>
          <span className="text-slate-500">
            Market: <span className="font-mono text-slate-300">${marketValue ?? '--'}</span>
          </span>
          {indicator && (
            <span className={`ml-auto text-xs font-medium ${indicator.cls}`}>
              {indicator.label}
            </span>
          )}
        </div>
      )}
    </div>
  )
}
