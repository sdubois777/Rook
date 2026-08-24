import { AlertTriangle } from 'lucide-react'
import { useDraftStore } from '../../stores/draft'

/**
 * Names the reason Rook is REFUSING this user's extension updates (#461).
 *
 * The extension cannot report this itself: postDraftEvent in
 * extension/src/utils/api.js discards the response status, so a refusal looks
 * exactly like success everywhere in the product — the extension popup still
 * reads "Connected" and still reads "relaying", while the draft room simply
 * never moves. The server knows both the user and the reason at the moment it
 * refuses, so it pushes them here.
 *
 * Renders nothing in the normal case, and clears itself as soon as any draft
 * update gets through (see markExtensionEvent in the draft store) — leaving a
 * warning up after updates resume would be stating something untrue.
 */
export default function ExtensionBlockedBanner() {
  const blocked = useDraftStore((s) => s.extensionBlocked)
  if (!blocked?.message) return null

  return (
    <div
      role="alert"
      className="flex items-start gap-2 px-4 py-2 bg-amber-500/10 border-b border-amber-500/30 text-amber-200"
    >
      <AlertTriangle size={14} className="shrink-0 mt-0.5" />
      <div className="text-xs leading-snug">
        <span>{blocked.message}</span>
        {blocked.code === 'live_draft_requires_paid_plan' && (
          <>
            {' '}
            <a href="/pricing" className="underline hover:text-amber-100">
              See plans
            </a>
          </>
        )}
      </div>
    </div>
  )
}
