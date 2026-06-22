import { useDraftStore } from '../../stores/draft'
import PositionBadge from '../shared/PositionBadge'
import { POSITION_SLOTS, assignToSlot } from './MyRoster'

// A team is dangerous when the total ai_bid_ceiling of its roster crosses this
// line — catches the "CMC + Jonathan Taylor + Puka Nacua" stack regardless of
// the prices actually paid.
export const THREAT_THRESHOLD = 150

// Position slot grid with FLEX/BN overflow, reusing MyRoster's tested
// assignment logic. Used identically for your team AND opponents — every pick
// (yours via myRoster, opponents' via teamPicks) carries position/player_name/
// price, and a null position falls through to the bench.
function RosterSlotGrid({ roster }) {
  const grouped = {}
  for (const pick of roster) {
    assignToSlot(pick, grouped, POSITION_SLOTS)
  }

  return (
    <div className="space-y-1">
      {Object.entries(POSITION_SLOTS).map(([pos, count]) => {
        const fills = grouped[pos] || []
        const slots = []
        for (let i = 0; i < count; i++) {
          const pick = fills[i]
          slots.push(
            <div
              key={`${pos}-${i}`}
              className={`flex items-center gap-2 px-3 py-1.5 rounded ${
                pick ? 'bg-surface-2' : 'border border-dashed border-border'
              }`}
            >
              <span className="text-[10px] text-slate-600 w-6 font-mono">{pos}</span>
              {pick ? (
                <>
                  <PositionBadge position={pick.position} />
                  <span className="text-sm text-slate-300 flex-1 truncate">
                    {pick.player_name}
                  </span>
                  <span className="text-xs font-mono text-slate-500">${pick.price}</span>
                </>
              ) : (
                <span className="text-xs text-slate-700 italic">Empty</span>
              )}
            </div>
          )
        }
        return slots
      })}
    </div>
  )
}

export default function TeamRosterPanel() {
  const teamPicks = useDraftStore((s) => s.teamPicks)
  const teamThreatScores = useDraftStore((s) => s.teamThreatScores)
  const teamsState = useDraftStore((s) => s.teamsState)
  const myTeamName = useDraftStore((s) => s.myTeamName)
  const selectedTeam = useDraftStore((s) => s.selectedTeam)
  const setSelectedTeam = useDraftStore((s) => s.setSelectedTeam)
  const myRoster = useDraftStore((s) => s.myRoster)

  // All teams seen in the draft — union of scraped budgets and recorded picks,
  // so a team with picks but no scraped budget (or vice versa) still shows.
  const allTeams = Array.from(
    new Set([
      ...Object.keys(teamsState || {}),
      ...Object.keys(teamPicks || {}),
      ...(myTeamName ? [myTeamName] : []),
    ])
  ).sort((a, b) => {
    if (a === myTeamName) return -1
    if (b === myTeamName) return 1
    return (teamThreatScores[b] || 0) - (teamThreatScores[a] || 0)
  })

  const activeTeam = selectedTeam || myTeamName || allTeams[0] || null
  const isMyTeam = activeTeam != null && activeTeam === myTeamName

  // My team uses myRoster (has position slots); opponents use teamPicks.
  const roster = isMyTeam ? myRoster : teamPicks[activeTeam] || []

  const teamInfo = teamsState?.[activeTeam]
  const threatScore = teamThreatScores[activeTeam] || 0
  const isHighThreat = threatScore >= THREAT_THRESHOLD

  return (
    <div className="flex flex-col h-full">
      {/* Team selector */}
      <div className="p-3 border-b border-border">
        <h3 className="text-xs font-medium text-slate-500 uppercase tracking-wider mb-2">
          Team Rosters
        </h3>
        <select
          aria-label="Select team"
          value={activeTeam || ''}
          onChange={(e) => setSelectedTeam(e.target.value)}
          className="w-full bg-surface-2 text-white border border-border rounded px-3 py-2 text-sm"
        >
          {allTeams.length === 0 && <option value="">No teams yet</option>}
          {allTeams.map((team) => (
            <option key={team} value={team}>
              {(teamThreatScores[team] || 0) >= THREAT_THRESHOLD ? '⚠️ ' : ''}
              {team}
              {team === myTeamName ? ' (you)' : ''}
            </option>
          ))}
        </select>
      </div>

      {/* Team info bar */}
      <div className="p-3 border-b border-border">
        {isHighThreat && (
          <div className="text-amber-400 text-xs mb-2">
            ⚠️ High threat — ${threatScore} ceiling value
          </div>
        )}
        <div className="flex justify-between text-sm text-slate-400">
          <span>
            Budget: <span className="font-mono">${teamInfo?.budget ?? '--'}</span>
          </span>
          <span>
            <span className="font-mono">{teamInfo?.slotsUsed ?? '--'}</span>/
            {teamInfo?.totalSlots ?? 16} slots
          </span>
        </div>
        {/* Budget bar */}
        <div className="mt-2 h-1.5 bg-surface-0 rounded-full overflow-hidden">
          <div
            className="h-full rounded-full bg-blue-500"
            style={{
              width: `${Math.min(((teamInfo?.budget || 0) / 200) * 100, 100)}%`,
            }}
          />
        </div>
      </div>

      {/* Roster slot grid — identical for your team and opponents */}
      <div className="flex-1 overflow-y-auto p-2">
        {roster.length === 0 ? (
          <div className="text-slate-500 text-sm p-3 text-center">No picks yet</div>
        ) : (
          <RosterSlotGrid roster={roster} />
        )}
      </div>
    </div>
  )
}
