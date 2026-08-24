import { useEffect, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import { useDraftStore } from '../../stores/draft'
import { formatElapsed } from '../../utils/elapsed'

/**
 * Whether the browser extension is actually delivering draft updates (#461).
 *
 * This is deliberately SEPARATE from the connection dot beside it. That dot
 * reports the browser-to-Rook socket, which stays green whether or not a single
 * draft update has ever arrived — so a user whose extension is not installed,
 * not running on the draft page, orphaned by a Chrome update, or refused by the
 * server saw a room that looked perfectly healthy and simply never moved. That
 * is the state a customer reported as "not synced to the draft on ESPN", and
 * nothing in the product could distinguish it from a working draft.
 *
 * Deliberately NOT an alarm on silence. Every reader can be legitimately quiet:
 * Sleeper only emits on a real pick, the Yahoo auction reader only on a change,
 * and the ESPN auction reader every five seconds while a nominee is up. A
 * "nothing for N seconds" warning would fire during normal drafts and train
 * people to ignore it. So this states the plain fact — how long ago the last
 * update arrived — and reserves a warning for the one unambiguous case: nothing
 * has EVER arrived.
 */

/** Re-render cadence. The label is coarse, so this need not be per-second. */
const TICK_MS = 5000

const NOTHING_YET_HELP =
  'Rook has not received any draft updates from the browser extension for this ' +
  'draft. Check that the extension is installed, that it is open on your draft ' +
  'page, and that your draft token is saved in the extension.'

export default function ExtensionStatus() {
  const lastExtensionEventAt = useDraftStore((s) => s.lastExtensionEventAt)
  // The clock is held in state rather than read during render: reading it while
  // rendering is impure and yields a value React cannot reason about.
  const [now, setNow] = useState(null)

  // ONE interval for the component's lifetime. Deliberately not keyed on the
  // last-event time: during an ESPN snake draft an update lands about every
  // second, so a keyed effect would tear the timer down and rebuild it before it
  // ever fired.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS)
    return () => clearInterval(id)
  }, [])

  if (lastExtensionEventAt == null) {
    return (
      <span
        className="flex items-center gap-1 text-xs text-amber-400"
        title={NOTHING_YET_HELP}
      >
        <AlertTriangle size={12} className="shrink-0" />
        <span>No draft data yet</span>
      </span>
    )
  }

  // A tick that predates the newest event yields a negative duration, which
  // formatElapsed floors at "0s" — the right answer for an update that just
  // landed. Before the first tick `now` is null, which reads the same way.
  const elapsed = formatElapsed((now ?? lastExtensionEventAt) - lastExtensionEventAt)
  return (
    <span
      className="flex items-center gap-1.5 text-xs text-slate-500"
      title="How long ago the browser extension last sent a draft update. Gaps between picks are normal."
    >
      <span className="w-2 h-2 rounded-full bg-emerald-500 shrink-0" />
      <span className="hidden sm:inline">Draft data</span>
      <span>{elapsed} ago</span>
    </span>
  )
}
