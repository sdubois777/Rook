import PositionBadge from './PositionBadge'
import FlagBadge from './FlagBadge'

export default function PlayerCardCompact({ player, onClick }) {
  const topFlag = player.flags?.[0]

  return (
    <div
      onClick={() => onClick?.(player.id)}
      className="flex items-center gap-3 px-4 py-2.5 hover:bg-surface-3 cursor-pointer transition-colors border-b border-border/50"
    >
      <PositionBadge position={player.position} />
      <span className="font-medium text-sm text-slate-200 min-w-[140px]">
        {player.name}
      </span>
      <span className="text-xs text-slate-500 w-10">{player.team_abbr}</span>
      <span className="text-xs text-slate-500 w-10">T{player.tier || '?'}</span>
      <span className="text-sm text-blue-400 font-mono w-12 text-right">
        {player.recommended_bid_ceiling != null
          ? `$${player.recommended_bid_ceiling.toFixed(0)}`
          : '--'}
      </span>
      <div className="ml-auto">
        {topFlag && <FlagBadge flagType={topFlag.flag_type} compact />}
      </div>
    </div>
  )
}
