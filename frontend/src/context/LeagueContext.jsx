import { createContext, useContext, useState, useCallback } from 'react'
import { useDraftStore } from '../stores/draft'

const STORAGE_KEY = 'selectedLeague'

// Default value is AUCTION — so any component read OUTSIDE a provider (e.g. in a
// unit test that doesn't wrap one) gets the existing auction UI unchanged.
export const LeagueContext = createContext({
  selectedLeague: null,
  setSelectedLeague: () => {},
  isSnake: false,
  isAuction: false,
  scoringFormat: 'ppr',
})

function readSaved() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export function LeagueProvider({ children }) {
  const [selectedLeague, setSelected] = useState(readSaved)

  const setSelectedLeague = useCallback((league) => {
    try {
      if (league) localStorage.setItem(STORAGE_KEY, JSON.stringify(league))
      else localStorage.removeItem(STORAGE_KEY)
    } catch {
      // localStorage unavailable (private mode) — state still updates in-memory
    }
    setSelected(league)
  }, [])

  // The LIVE draft's detected format (propagated by the backend into the store)
  // is the single source of truth for the panel selector — it OVERRIDES the
  // statically-selected sidebar league, so an auction draft opened under a
  // snake-selected league self-corrects to auction (and vice versa). Falls back
  // to the selected league before the first format-defining event arrives.
  const liveDraftType = useDraftStore((s) => s.liveDraftType)
  const draftType = liveDraftType || selectedLeague?.draft_type
  const value = {
    selectedLeague,
    setSelectedLeague,
    isSnake: draftType === 'snake',
    isAuction: draftType === 'auction',
    scoringFormat: selectedLeague?.scoring || 'ppr',
  }

  return <LeagueContext.Provider value={value}>{children}</LeagueContext.Provider>
}

// Convenience hook.
export function useLeague() {
  return useContext(LeagueContext)
}
