import { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { Bug, Lightbulb, X, CheckCircle2 } from 'lucide-react'
import { submitFeedback } from '../api/feedback'
import { useLeague } from '../context/LeagueContext'

/**
 * Bug report / feature suggestion form. Submitting files a GitHub issue in the
 * project repo (backend/routers/feedback.py), which is the backlog.
 *
 * The setup fields — page, browser, viewport, league platform, draft type, scoring
 * format — are captured here rather than asked for. Those are exactly the details a
 * reporter omits and an investigator needs, and the user already told us all of them
 * by having the app open.
 */
const KINDS = [
  { value: 'bug', label: 'Something is broken', icon: Bug },
  { value: 'idea', label: 'I have a suggestion', icon: Lightbulb },
]

const TITLE_MAX = 160
const DESCRIPTION_MAX = 6000

const PLACEHOLDERS = {
  bug: 'What did you do, what did you expect, and what happened instead? Player names and exact numbers help a lot.',
  idea: "What would you like Rook to do, and what would you use it for?",
}

/**
 * Mounted only while the dialog is open (see App.jsx), so every open starts from a
 * fresh component. That is what resets the form — there is no reset effect, and a
 * previous submission cannot bleed into the next report.
 */
export default function FeedbackModal({ onClose }) {
  const location = useLocation()
  const { selectedLeague, draftType, scoringFormat } = useLeague()
  const [kind, setKind] = useState('bug')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // Just "did it send". The endpoint deliberately returns no issue number or link, so
  // there is nothing else to hold — see FeedbackResponse in backend/routers/feedback.py.
  const [sent, setSent] = useState(false)

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const canSubmit = title.trim().length >= 3 && description.trim().length >= 10 && !busy

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!canSubmit) return
    setBusy(true)
    setError('')
    try {
      await submitFeedback({
        kind,
        title: title.trim().slice(0, TITLE_MAX),
        description: description.trim().slice(0, DESCRIPTION_MAX),
        page: location.pathname + location.search,
        user_agent: navigator.userAgent?.slice(0, 300),
        viewport: `${window.innerWidth}x${window.innerHeight}`,
        league_platform: selectedLeague?.platform || null,
        draft_type: draftType || null,
        scoring_format: scoringFormat || null,
      })
      setSent(true)
    } catch (err) {
      setError(
        err?.response?.data?.message ||
        'Could not send your report. Please try again in a moment.'
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-end sm:items-center justify-center bg-black/60 p-0 sm:p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Report a bug or suggest a feature"
        className="w-full sm:max-w-lg bg-surface-1 border border-border rounded-t-xl sm:rounded-xl shadow-xl max-h-[92vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <h2 className="text-sm font-semibold text-slate-100">
            {sent ? 'Report sent' : 'Report a bug or suggest a feature'}
          </h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="flex items-center justify-center min-h-11 min-w-11 lg:min-h-0 lg:min-w-0 text-slate-400 hover:text-slate-200"
          >
            <X size={18} />
          </button>
        </div>

        {sent ? (
          <div className="px-4 py-6 space-y-3">
            <div className="flex items-start gap-2">
              <CheckCircle2 size={18} className="text-emerald-400 shrink-0 mt-0.5" />
              {/* Generic on purpose. Where the report goes is an internal detail, and a
                  tracker link is useless to someone without repository access. */}
              <p className="text-sm text-slate-200">
                Thanks — your report has been sent.
              </p>
            </div>
            <button
              onClick={onClose}
              className="w-full mt-2 px-3 py-2 min-h-11 lg:min-h-0 text-sm bg-surface-2 text-slate-200 border border-border rounded hover:bg-surface-3 transition-colors"
            >
              Done
            </button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="px-4 py-4 space-y-4">
            <div className="grid grid-cols-2 gap-2">
              {KINDS.map(({ value, label, icon: Icon }) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setKind(value)}
                  aria-pressed={kind === value}
                  className={`flex items-center gap-2 px-3 py-2 min-h-11 lg:min-h-0 text-xs rounded border transition-colors ${
                    kind === value
                      ? 'border-brand-accent/60 bg-brand/10 text-slate-100'
                      : 'border-border bg-surface-2 text-slate-400 hover:text-slate-200'
                  }`}
                >
                  <Icon size={14} className="shrink-0" />
                  {label}
                </button>
              ))}
            </div>

            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-slate-500">
                One-line summary
              </span>
              <input
                autoFocus
                type="text"
                value={title}
                maxLength={TITLE_MAX}
                onChange={(e) => setTitle(e.target.value)}
                placeholder={kind === 'bug' ? 'Gap column shows the wrong number' : 'Show bye weeks on the draft board'}
                className="w-full px-3 py-2 min-h-11 lg:min-h-0 text-sm bg-surface-2 text-slate-200 border border-border rounded focus:outline-none focus:border-brand-accent/60 placeholder-slate-600"
              />
            </label>

            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-slate-500">Details</span>
              <textarea
                value={description}
                rows={6}
                maxLength={DESCRIPTION_MAX}
                onChange={(e) => setDescription(e.target.value)}
                placeholder={PLACEHOLDERS[kind]}
                className="w-full px-3 py-2 text-sm bg-surface-2 text-slate-200 border border-border rounded focus:outline-none focus:border-brand-accent/60 placeholder-slate-600 resize-y"
              />
            </label>

            <p className="text-[11px] text-slate-500 leading-snug">
              Your current page, browser, screen size and league settings are attached
              automatically. Your email address is not sent.
            </p>

            {error && (
              <p className="text-xs text-red-400" role="alert">{error}</p>
            )}

            <div className="flex gap-2">
              <button
                type="button"
                onClick={onClose}
                className="flex-1 px-3 py-2 min-h-11 lg:min-h-0 text-sm bg-surface-2 text-slate-300 border border-border rounded hover:bg-surface-3 transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!canSubmit}
                className="flex-1 px-3 py-2 min-h-11 lg:min-h-0 text-sm font-semibold bg-brand text-white rounded hover:bg-brand-hover transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {busy ? 'Sending...' : 'Send report'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}
