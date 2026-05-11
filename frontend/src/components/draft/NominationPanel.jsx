import { useState, useCallback } from 'react'
import { useDraftStore } from '../../stores/draft'
import { nominatePlayer } from '../../api/draft'
import PositionBadge from '../shared/PositionBadge'
import ValueComparisonBar from '../shared/ValueComparisonBar'
import SearchInput from '../shared/SearchInput'

export default function NominationPanel() {
  const rec = useDraftStore((s) => s.recommendation)
  const currentBid = useDraftStore((s) => s.currentBid)
  const comboAlerts = useDraftStore((s) => s.comboAlerts)
  const availablePlayers = useDraftStore((s) => s.availablePlayers)
  const [nomSearch, setNomSearch] = useState('')
  const [nominating, setNominating] = useState(false)

  const handleSearch = useCallback((value) => {
    setNomSearch(value)
  }, [])

  const handleNominate = async (player) => {
    setNominating(true)
    try {
      await nominatePlayer(player.yahoo_player_id, 1)
      setNomSearch('')
    } catch {
      // Nomination failed
    }
    setNominating(false)
  }

  // Filter available players for nomination search
  const nomResults = nomSearch.length >= 2
    ? availablePlayers
        .filter((p) =>
          p.name?.toLowerCase().includes(nomSearch.toLowerCase())
        )
        .slice(0, 5)
    : []

  const hasNomination = rec || currentBid

  return (
    <div className="h-full flex flex-col p-4 overflow-y-auto">
      <h3 className="text-sm font-medium text-slate-400 uppercase tracking-wider mb-3">
        Current Nomination
      </h3>

      {hasNomination ? (
        <>
          {/* Current nominee info */}
          <div className="mb-3">
            <div className="flex items-center gap-2 mb-1">
              <PositionBadge position={rec?.position} />
              <span className="text-lg font-medium text-slate-200">
                {rec?.player_name || currentBid?.player_name || 'Unknown'}
              </span>
            </div>
          </div>

          {/* Current bid */}
          {currentBid && (
            <div className="bg-[#1c1f2e] rounded-lg p-3 mb-3">
              <div className="text-xs text-slate-500 mb-1">Current Bid</div>
              <div className="text-2xl font-mono font-bold text-amber-400">
                ${currentBid.current_bid}
              </div>
              <div className="text-xs text-slate-500 mt-1">
                by {currentBid.current_bidder}
              </div>
            </div>
          )}

          {/* Value comparison */}
          {rec && (
            <div className="mb-3">
              <ValueComparisonBar
                systemValue={rec.system_value}
                marketValue={rec.market_value}
              />
            </div>
          )}
        </>
      ) : (
        <div className="flex-1 flex items-center justify-center text-slate-600">
          <p>Waiting for nomination...</p>
        </div>
      )}

      {/* Combo alerts */}
      {comboAlerts.length > 0 && (
        <div className="space-y-1 mb-3">
          <h4 className="text-xs font-medium text-amber-400 uppercase tracking-wider">
            Opponent Combos
          </h4>
          {comboAlerts.slice(-3).map((alert, i) => (
            <div
              key={i}
              className="text-xs text-amber-400 bg-amber-500/10 border border-amber-500/20 rounded px-2 py-1"
            >
              {alert.team_id}: {alert.combos?.join(', ')}
            </div>
          ))}
        </div>
      )}

      {/* Nomination search */}
      <div className="mt-auto">
        <h4 className="text-xs font-medium text-slate-500 uppercase tracking-wider mb-2">
          Nominate a Player
        </h4>
        <SearchInput
          placeholder="Search to nominate..."
          onSearch={handleSearch}
          delay={200}
        />
        {nomResults.length > 0 && (
          <div className="mt-1 bg-[#1c1f2e] border border-[#2d3148] rounded-lg overflow-hidden">
            {nomResults.map((p) => (
              <button
                key={p.id || p.yahoo_player_id}
                onClick={() => handleNominate(p)}
                disabled={nominating}
                className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-[#222539] transition-colors border-b border-[#2d3148]/50 last:border-b-0"
              >
                <PositionBadge position={p.position} />
                <span className="text-sm text-slate-300">{p.name}</span>
                <span className="text-xs text-slate-600 ml-auto">
                  ${p.ai_bid_ceiling ?? p.recommended_bid_ceiling?.toFixed(0) ?? '--'}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
