import { useState, useEffect } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { useDraftStore } from '../../stores/draft'
import { getOpponentBudgets } from '../../api/draft'

function getThreatColor(score) {
  if (score >= 60) return 'bg-red-500'
  if (score >= 30) return 'bg-amber-500'
  return 'bg-emerald-500'
}

export default function OpponentTracker() {
  const [open, setOpen] = useState(false)
  const [opponents, setOpponents] = useState({})
  const comboAlerts = useDraftStore((s) => s.comboAlerts)
  const picks = useDraftStore((s) => s.picks)

  // Refresh opponent data when picks change
  useEffect(() => {
    if (!open) return
    getOpponentBudgets()
      .then((data) => setOpponents(data.opponents || {}))
      .catch(() => {})
  }, [open, picks.length])

  return (
    <>
      {/* Toggle button */}
      <button
        onClick={() => setOpen(!open)}
        className="fixed right-0 top-1/2 -translate-y-1/2 z-40 bg-[#161822] border border-[#2d3148] border-r-0 rounded-l-lg px-1 py-3 text-slate-500 hover:text-slate-300 transition-colors"
      >
        {open ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
      </button>

      {/* Sidebar */}
      <div
        className={`fixed right-0 top-0 h-screen w-72 bg-[#161822] border-l border-[#2d3148] z-30 transform transition-transform ${
          open ? 'translate-x-0' : 'translate-x-full'
        }`}
      >
        <div className="p-4 h-full overflow-y-auto">
          <h3 className="text-sm font-medium text-slate-400 uppercase tracking-wider mb-4">
            Opponents
          </h3>

          {Object.keys(opponents).length === 0 ? (
            <p className="text-xs text-slate-600">No opponent data yet</p>
          ) : (
            <div className="space-y-3">
              {Object.entries(opponents)
                .sort(([, a], [, b]) => (b.threat_score || 0) - (a.threat_score || 0))
                .map(([teamId, opp]) => (
                  <div
                    key={teamId}
                    className="bg-[#1c1f2e] rounded-lg p-3 border border-[#2d3148]/50"
                  >
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-sm text-slate-300 font-medium">
                        {teamId}
                      </span>
                      <span className="text-xs font-mono text-slate-400">
                        ${opp.budget}
                      </span>
                    </div>

                    {/* Threat bar */}
                    <div className="flex items-center gap-2 mb-1">
                      <div className="flex-1 h-1.5 bg-[#0f1117] rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full ${getThreatColor(opp.threat_score || 0)}`}
                          style={{ width: `${Math.min(opp.threat_score || 0, 100)}%` }}
                        />
                      </div>
                      <span className="text-[10px] text-slate-600 w-6 text-right">
                        {opp.threat_score || 0}
                      </span>
                    </div>

                    <div className="text-[10px] text-slate-600">
                      {opp.roster_count || 0} picks
                    </div>

                    {/* Combos */}
                    {opp.combos?.length > 0 && (
                      <div className="mt-2 space-y-1">
                        {opp.combos.map((c, i) => (
                          <div
                            key={i}
                            className="text-[10px] text-amber-400 bg-amber-500/10 rounded px-1.5 py-0.5"
                          >
                            {c}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
            </div>
          )}

          {/* Recent combo alerts */}
          {comboAlerts.length > 0 && (
            <div className="mt-4">
              <h4 className="text-xs text-amber-400 uppercase tracking-wider mb-2">
                Recent Alerts
              </h4>
              {comboAlerts.slice(-5).map((alert, i) => (
                <div
                  key={i}
                  className="text-[10px] text-amber-300 bg-amber-500/10 border border-amber-500/20 rounded px-2 py-1 mb-1"
                >
                  {alert.team_id}: {alert.combos?.join(', ')}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  )
}
