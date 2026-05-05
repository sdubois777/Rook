import PositionBadge from './PositionBadge'
import FlagBadge from './FlagBadge'
import ValueComparisonBar from './ValueComparisonBar'

export default function PlayerCardExpanded({ player, onClick }) {
  return (
    <div
      onClick={() => onClick?.(player.id)}
      className="px-4 py-3 hover:bg-[#222539] cursor-pointer transition-colors border-b border-[#2d3148]/50"
    >
      <div className="flex items-center gap-3">
        <PositionBadge position={player.position} />
        <div className="min-w-[160px]">
          <span className="font-medium text-sm text-slate-200">{player.name}</span>
          <span className="text-xs text-slate-500 ml-2">{player.team_abbr}</span>
        </div>

        <span className="text-xs text-slate-500 w-10">T{player.tier || '?'}</span>

        {/* Bid ceiling */}
        <div className="text-right w-16">
          <div className="text-sm text-blue-400 font-mono">
            {player.recommended_bid_ceiling != null
              ? `$${player.recommended_bid_ceiling.toFixed(0)}`
              : '--'}
          </div>
          <div className="text-[10px] text-slate-500">ceiling</div>
        </div>

        {/* System value */}
        <div className="text-right w-16">
          <div className="text-sm text-slate-300 font-mono">
            {player.baseline_value != null
              ? `$${player.baseline_value.toFixed(0)}`
              : '--'}
          </div>
          <div className="text-[10px] text-slate-500">system</div>
        </div>

        {/* Market value */}
        <div className="text-right w-16">
          <div className="text-sm text-slate-300 font-mono">
            {player.market_value != null
              ? `$${player.market_value.toFixed(0)}`
              : '--'}
          </div>
          <div className="text-[10px] text-slate-500">market</div>
        </div>

        {/* Value gap */}
        <div className="w-24">
          {player.baseline_value != null && player.market_value != null ? (
            <ValueComparisonBar
              systemValue={player.baseline_value}
              marketValue={player.market_value}
            />
          ) : (
            <span className="text-xs text-slate-500">--</span>
          )}
        </div>

        {/* Flags */}
        <div className="flex gap-1 ml-auto flex-wrap justify-end max-w-[200px]">
          {(player.flags || []).slice(0, 3).map((f, i) => (
            <FlagBadge key={i} flagType={f.flag_type} compact />
          ))}
          {player.breakout_flag && (
            <span className="inline-flex items-center px-1.5 py-0.5 text-[10px] font-medium rounded-full bg-yellow-500/15 text-yellow-400">
              Breakout
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
