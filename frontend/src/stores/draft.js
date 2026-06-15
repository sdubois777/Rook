import { create } from 'zustand'
import {
  startDraft as apiStartDraft,
  getDraftState,
  getAvailablePlayers,
  endDraft as apiEndDraft,
} from '../api/draft'

/** Total seconds remaining from a "M:SS" clock string ("0:19" -> 19). */
export function parseClockSeconds(clock) {
  if (!clock) return null
  const parts = String(clock).split(':')
  if (parts.length !== 2) return null
  const mins = parseInt(parts[0], 10)
  const secs = parseInt(parts[1], 10)
  if (Number.isNaN(mins) || Number.isNaN(secs)) return null
  return mins * 60 + secs
}

export const useDraftStore = create((set, get) => ({
  // Connection
  phase: 'setup', // 'setup' | 'live' | 'ended'
  wsStatus: 'disconnected',
  bridgeStatus: null,

  // Current nomination
  recommendation: null,
  currentBid: null,
  currentNomination: null, // { playerName, posTeam, currentBid, clock, secondsRemaining }
  teamsState: {}, // live scraped team budgets from the extension poller

  // Draft state
  myBudget: 200,
  myRoster: [],
  myTeamName: null, // your team's display name — used to detect picks you win
  rosterSlotsRemaining: 16,
  spendable: 200,
  positionalCounts: {},

  // Picks + opponents
  picks: [],
  opponentBudgets: {},
  comboAlerts: [],

  // Available players
  availablePlayers: [],
  availableFilter: { position: '', search: '' },

  // --- Actions ---

  startDraft: async (teamId, draftRoomUrl) => {
    const startResp = await apiStartDraft(teamId, draftRoomUrl)

    // Load initial state + available players in parallel
    const [state, board] = await Promise.all([
      getDraftState(),
      getAvailablePlayers(),
    ])

    // Flatten tiers into a single list
    const tiers = board?.tiers || {}
    const players = Object.values(tiers).flat()

    set({
      phase: 'live',
      // Team name used to detect picks you win — prefer the backend echo,
      // fall back to the id entered at setup.
      myTeamName: startResp?.team_name || teamId || null,
      myBudget: state.your_remaining_budget,
      myRoster: state.your_roster || [],
      rosterSlotsRemaining: state.roster_slots_remaining,
      spendable: state.spendable_on_next_player,
      positionalCounts: state.positional_counts || {},
      availablePlayers: players,
    })
  },

  setMyTeamName: (name) => set({ myTeamName: name }),

  setRecommendation: (rec) => {
    set({
      recommendation: rec,
      // Update budget from recommendation if included
      ...(rec.budget_summary
        ? {
            myBudget: rec.budget_summary.your_remaining,
            spendable: rec.budget_summary.spendable_on_this_player,
            rosterSlotsRemaining: rec.budget_summary.roster_slots_remaining,
          }
        : {}),
    })
  },

  // A new player hit the block (extension nomination event). Show the card
  // immediately; the AI recommendation arrives a beat later from the engine.
  setNomination: (payload) =>
    set({
      currentNomination: {
        playerName: payload.player_name,
        posTeam: payload.pos_team,
        currentBid: payload.opening_bid,
        clock: payload.clock,
        secondsRemaining: parseClockSeconds(payload.clock),
      },
      currentBid: {
        current_bid: payload.opening_bid,
        player_name: payload.player_name,
      },
      recommendation: null,
    }),

  updateBid: (bid) =>
    set((s) => ({
      currentBid: bid,
      currentNomination: s.currentNomination
        ? {
            ...s.currentNomination,
            currentBid: bid.current_bid ?? s.currentNomination.currentBid,
            clock: bid.clock ?? s.currentNomination.clock,
            secondsRemaining:
              bid.clock != null
                ? parseClockSeconds(bid.clock)
                : s.currentNomination.secondsRemaining,
          }
        : s.currentNomination,
    })),

  updateClock: (payload) =>
    set((s) => ({
      currentNomination: s.currentNomination
        ? {
            ...s.currentNomination,
            clock: payload.clock,
            secondsRemaining:
              payload.seconds_remaining ?? parseClockSeconds(payload.clock),
          }
        : s.currentNomination,
    })),

  updateTeams: (teams) => set({ teamsState: teams || {} }),

  recordPick: (pick) => {
    const state = get()
    const pickName = (pick.player_name || '').toLowerCase()

    // DEDUP: a player can only be drafted once, so a repeat of the same name
    // is always a duplicate delivery (e.g. a double-mounted socket in dev, or
    // a relay retry). Ignore it — otherwise the player is added to the roster
    // twice and the available list is filtered on a second, stale pass.
    if (
      pickName &&
      state.picks.some((p) => p.player_name?.toLowerCase() === pickName)
    ) {
      return
    }

    const newPicks = [...state.picks, pick]

    // Find the available entry (for position lookup) before removing it.
    const fromAvailable = state.availablePlayers.find(
      (p) => p.name?.toLowerCase() === pickName
    )

    // Remove ONLY the drafted player from the available list — never clear it.
    // Relayed picks carry only a name (no id), so match on id OR name
    // (case-insensitive, since DOM names may differ in case).
    const newAvailable = state.availablePlayers.filter(
      (p) =>
        p.yahoo_player_id !== pick.player_id &&
        p.id !== pick.player_id &&
        p.name?.toLowerCase() !== pickName
    )

    // Clear current recommendation + bid + nomination after pick confirmed
    const updates = {
      picks: newPicks,
      availablePlayers: newAvailable,
      recommendation: null,
      currentBid: null,
      currentNomination: null,
      ...(pick.teams_snapshot ? { teamsState: pick.teams_snapshot } : {}),
    }

    // Did we win this player? The relay carries `winner` (team display name);
    // the engine path may set `is_yours`. Match winner against our team name.
    const isYours =
      pick.is_yours ||
      (state.myTeamName &&
        pick.winner &&
        pick.winner.toLowerCase() === state.myTeamName.toLowerCase())

    if (isYours) {
      const price = pick.final_price || pick.price || 0
      updates.myRoster = [
        ...state.myRoster,
        {
          player_id: pick.player_id,
          player_name: pick.player_name,
          position: pick.position || fromAvailable?.position || null,
          price,
        },
      ]
      updates.myBudget = state.myBudget - price
      updates.rosterSlotsRemaining = state.rosterSlotsRemaining - 1
    }

    set(updates)
  },

  addComboAlert: (alert) => {
    set((s) => ({ comboAlerts: [...s.comboAlerts, alert] }))
  },

  setBridgeStatus: (status) => set({ bridgeStatus: status }),

  setWsStatus: (status) => set({ wsStatus: status }),

  refreshState: async () => {
    const state = await getDraftState()
    set({
      myBudget: state.your_remaining_budget,
      myRoster: state.your_roster || [],
      rosterSlotsRemaining: state.roster_slots_remaining,
      spendable: state.spendable_on_next_player,
      positionalCounts: state.positional_counts || {},
    })
  },

  endDraft: async () => {
    await apiEndDraft()
    set({ phase: 'ended' })
  },

  setAvailableFilter: (filter) => {
    set((s) => ({
      availableFilter: { ...s.availableFilter, ...filter },
    }))
  },
}))
