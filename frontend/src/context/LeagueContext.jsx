/* eslint-disable react-refresh/only-export-components --
 * PRE-EXISTING, not introduced here: this file has always exported the context object and
 * the useLeague hook alongside the LeagueProvider component, and eslint has always flagged
 * both. It goes unnoticed because CI runs `npm test` + build, not `npm run lint` — only the
 * local changed-files hook sees it.
 *
 * The rule's fix is to move the non-component exports to their own module, but `useLeague`
 * is imported from this path by 21 files, so that refactor belongs in its own commit rather
 * than inflating a UI-behaviour diff. Fast refresh degrades to a full reload for consumers
 * of this file in dev; nothing else is affected.
 */
import { createContext, useContext, useState, useCallback } from 'react'
import { useDraftStore } from '../stores/draft'

const STORAGE_KEY = 'selectedLeague'
// Client-only manual format choice, used ONLY when no league is synced (see the
// precedence chain below). Deliberately NOT in usePreferencesStore — that store is
// server-backed (loadStrategy/loadWatchlist hit the API) and this needs no round trip.
const OVERRIDE_KEY = 'leagueFormatOverride'

// Snake PPR is the default for a user with no synced league: it is the most common
// format, and the previous behaviour was not a decision at all — draftType resolved
// from the league alone, so with no league BOTH isSnake and isAuction were false and
// every `isSnake ? snake : auction` consumer fell through to auction by accident.
export const DEFAULT_DRAFT_TYPE = 'snake'
export const DEFAULT_SCORING = 'ppr'

// The bare context default matches the provider's defaults. It used to be auction, on the
// reasoning that a component read OUTSIDE a provider should get "the existing UI unchanged"
// — but once the app's default is snake, two different defaults in one file is exactly the
// ambiguity that produced the accidental-auction bug. Production blast radius is zero:
// App.jsx wraps every authenticated route and all useLeague consumers sit inside it.
export const LeagueContext = createContext({
  selectedLeague: null,
  setSelectedLeague: () => {},
  draftType: DEFAULT_DRAFT_TYPE,
  isSnake: DEFAULT_DRAFT_TYPE === 'snake',
  isAuction: DEFAULT_DRAFT_TYPE === 'auction',
  scoringFormat: DEFAULT_SCORING,
  formatOverride: null,
  setFormatOverride: () => {},
  canChooseFormat: true,
})

function readJson(key) {
  try {
    const raw = localStorage.getItem(key)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export function LeagueProvider({ children }) {
  const [selectedLeague, setSelected] = useState(() => readJson(STORAGE_KEY))
  const [formatOverride, setOverride] = useState(() => readJson(OVERRIDE_KEY))

  const setSelectedLeague = useCallback((league) => {
    try {
      if (league) localStorage.setItem(STORAGE_KEY, JSON.stringify(league))
      else localStorage.removeItem(STORAGE_KEY)
    } catch {
      // localStorage unavailable (private mode) — state still updates in-memory
    }
    setSelected(league)
  }, [])

  // Merges, so the two selects can be changed independently.
  const setFormatOverride = useCallback((patch) => {
    setOverride((prev) => {
      const next = { ...(prev || {}), ...(patch || {}) }
      try {
        localStorage.setItem(OVERRIDE_KEY, JSON.stringify(next))
      } catch {
        // localStorage unavailable (private mode) — state still updates in-memory
      }
      return next
    })
  }, [])

  // PRECEDENCE, highest first. One chain, so "the manual toggle only applies when no
  // league is synced" falls out of the ordering rather than needing its own gate:
  //
  //   1. liveDraftType     — the LIVE draft's detected format, propagated by the backend
  //                          into the store. Always wins, so an auction draft opened under
  //                          a snake-selected league self-corrects (and vice versa).
  //   2. selectedLeague    — a synced league is authoritative. This is what makes the
  //                          manual override invisible to anyone with a real league.
  //   3. formatOverride    — the sidebar toggle, for users with nothing synced.
  //   4. DEFAULT_*         — snake / ppr.
  const liveDraftType = useDraftStore((s) => s.liveDraftType)
  const draftType =
    liveDraftType ||
    selectedLeague?.draft_type ||
    formatOverride?.draft_type ||
    DEFAULT_DRAFT_TYPE
  const scoringFormat =
    selectedLeague?.scoring || formatOverride?.scoring || DEFAULT_SCORING

  const value = {
    selectedLeague,
    setSelectedLeague,
    // The RESOLVED draft type, exported because DraftSetup passes it to POST /draft/start
    // to pick the backend recommendation engine — it must never re-derive its own default.
    draftType,
    // Exact complements now that draftType always resolves to a value. The old tri-state
    // (both false with no league) is what made auction the silent fallthrough.
    isSnake: draftType === 'snake',
    isAuction: draftType === 'auction',
    scoringFormat,
    formatOverride,
    setFormatOverride,
    // A synced league outranks the toggle, so offering it would be a lie.
    canChooseFormat: !selectedLeague,
  }

  return <LeagueContext.Provider value={value}>{children}</LeagueContext.Provider>
}

// Convenience hook.
export function useLeague() {
  return useContext(LeagueContext)
}
