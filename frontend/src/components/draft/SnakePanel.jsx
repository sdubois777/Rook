import { useDraftStore } from '../../stores/draft'

/**
 * Snake-draft turn panel (replaces NominationPanel when the league is snake).
 *
 * Three states:
 *   - YOUR TURN            green pulsing — you're on the clock
 *   - up in <= 2 picks     amber — get ready
 *   - someone else's pick  neutral — round/pick context
 */
export default function SnakePanel() {
  const isYourTurn = useDraftStore((s) => s.isYourTurn)
  const currentRound = useDraftStore((s) => s.currentRound)
  const currentPick = useDraftStore((s) => s.currentPick)
  const picksUntilYourTurn = useDraftStore((s) => s.picksUntilYourTurn)

  const roundPick =
    currentRound != null && currentPick != null
      ? `Round ${currentRound}, Pick ${currentPick}`
      : currentRound != null
      ? `Round ${currentRound}`
      : null

  if (isYourTurn) {
    return (
      <div className="h-full flex flex-col p-3">
        <h3 className="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">
          On The Clock
        </h3>
        <div className="flex-1 flex flex-col items-center justify-center">
          <div className="flex items-center gap-2 mb-1">
            <span className="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse" />
            <span className="text-xl font-bold text-emerald-400 tracking-wide">
              YOUR TURN
            </span>
          </div>
          {roundPick && <span className="text-sm text-slate-400">{roundPick}</span>}
        </div>
      </div>
    )
  }

  const soon = picksUntilYourTurn != null && picksUntilYourTurn <= 2

  return (
    <div className="h-full flex flex-col p-3">
      <h3 className="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">
        Snake Draft
      </h3>
      <div className="flex-1 flex flex-col items-center justify-center">
        {soon ? (
          <div className="flex items-center gap-2 mb-1">
            <span className="w-2.5 h-2.5 rounded-full bg-amber-500 animate-pulse" />
            <span className="text-lg font-bold text-amber-400">
              You're up in {picksUntilYourTurn}{' '}
              {picksUntilYourTurn === 1 ? 'pick' : 'picks'}
            </span>
          </div>
        ) : (
          <span className="text-base text-slate-300">
            {picksUntilYourTurn != null
              ? `You're up in ${picksUntilYourTurn} picks`
              : 'Waiting for the draft...'}
          </span>
        )}
        {roundPick && <span className="text-sm text-slate-500 mt-1">{roundPick}</span>}
      </div>
    </div>
  )
}
