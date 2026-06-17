import { createContext, useContext, useState, useCallback } from 'react'

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

  const draftType = selectedLeague?.draft_type
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
