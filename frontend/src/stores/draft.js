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

  // All picks grouped by winning team name — drives the Team Rosters panel.
  // { 'Stephen': [{ player_name, price, position, ceiling }], 'Team 3': [...] }
  teamPicks: {},
  // Sum of ai_bid_ceiling for each team's picks — the threat score.
  // { 'Team 3': 187, 'Stephen': 142 }
  teamThreatScores: {},
  // Team currently shown in the roster panel; null = default to my team.
  selectedTeam: null,

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
    console.debug('DraftMind: draftboard players loaded:', players.length)

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
      // Never wipe an already-loaded list with an empty fetch result — only
      // overwrite when the draftboard actually returned players.
      ...(players.length > 0 ? { availablePlayers: players } : {}),
    })
  },

  setMyTeamName: (name) => set({ myTeamName: name }),

  // Replace the available list, but never with an empty array (a failed or
  // empty /draftboard fetch must not wipe a populated list).
  setAvailablePlayers: (players) =>
    set((s) =>
      players && players.length > 0
        ? { availablePlayers: players }
        : s
    ),

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
    set((state) => {
      // The backend sometimes emits a bid_update (the real bid) just BEFORE the
      // nomination event, which carries opening_bid=1. Don't clobber a real,
      // higher bid for the SAME player with the opening bid.
      const sameHigherBid =
        state.currentBid?.player_name === payload.player_name &&
        state.currentBid?.current_bid > payload.opening_bid
      const bidAmount = sameHigherBid
        ? state.currentBid.current_bid
        : payload.opening_bid
      return {
        currentNomination: {
          playerName: payload.player_name,
          posTeam: payload.pos_team,
          currentBid: bidAmount,
          clock: payload.clock,
          secondsRemaining: parseClockSeconds(payload.clock),
        },
        currentBid: sameHigherBid
          ? state.currentBid
          : {
              current_bid: payload.opening_bid,
              player_name: payload.player_name,
            },
        recommendation: null,
      }
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
        : // No active nomination (e.g. the nomination event was missed or
          // already cleared) — reconstruct a minimal one from the bid so the
          // bid still shows instead of silently doing nothing.
          {
            playerName: bid.player_name ?? null,
            posTeam: null,
            currentBid: bid.current_bid,
            clock: bid.clock ?? null,
            secondsRemaining: parseClockSeconds(bid.clock),
          },
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

    // DEDUP (time-bounded): a duplicate delivery of the same pick (e.g. a
    // double-mounted socket in dev, or a relay retry) arrives within a moment,
    // so only ignore a same-name pick recorded in the last 2s. A stale pick
    // left in state from a previous session must NOT block a fresh one.
    const TWO_SECONDS = 2000
    const now = Date.now()
    if (
      pickName &&
      state.picks.some(
        (p) =>
          p.player_name?.toLowerCase() === pickName &&
          now - (p.timestamp || 0) < TWO_SECONDS
      )
    ) {
      console.debug('DraftMind: dedup blocked duplicate pick:', pick.player_name)
      return
    }

    // Did we win this player? The relay carries `winner` (team display name);
    // the engine path may set `is_yours`. Match winner against our team name.
    const isYours =
      pick.is_yours ||
      (state.myTeamName &&
        pick.winner &&
        pick.winner.toLowerCase() === state.myTeamName.toLowerCase())

    console.debug(
      'DraftMind: recordPick',
      pick.player_name,
      '| winner:',
      pick.winner,
      '| myTeamName:',
      state.myTeamName,
      '| isYours:',
      !!isYours,
    )

    const newPicks = [...state.picks, { ...pick, timestamp: now }]

    // Find the available entry (for position lookup) before removing it.
    const fromAvailable = state.availablePlayers.find(
      (p) => p.name?.toLowerCase() === pickName
    )

    // Remove ONLY the drafted player from the available list — never clear it.
    // Keep every player UNLESS it matches the sold pick by a real id or by name.
    // Note: relayed picks carry no player_id, and draftboard players have no
    // yahoo_player_id — so the id checks must be guarded, otherwise
    // `undefined !== undefined` would (wrongly) treat every player as a match
    // and wipe the whole list on the first sale.
    const newAvailable = state.availablePlayers.filter((p) => {
      const idMatch =
        pick.player_id != null &&
        (p.yahoo_player_id === pick.player_id || p.id === pick.player_id)
      const nameMatch = pickName !== '' && p.name?.toLowerCase() === pickName
      return !(idMatch || nameMatch)
    })

    // Group the pick under its winning team and recompute that team's threat
    // score (sum of ai_bid_ceiling). Threat is ceiling-based, not price-based,
    // so elite players flag a team even when bought cheaply.
    const winner = pick.winner || 'Unknown'
    const ceiling =
      fromAvailable?.ai_bid_ceiling ??
      fromAvailable?.recommended_bid_ceiling ??
      null
    const teamPicks = {
      ...state.teamPicks,
      [winner]: [
        ...(state.teamPicks[winner] || []),
        {
          player_name: pick.player_name,
          price: pick.final_price || pick.price || 0,
          position: pick.position || fromAvailable?.position || null,
          ceiling,
        },
      ],
    }
    const teamScore = teamPicks[winner].reduce(
      (sum, p) => sum + (p.ceiling || 0),
      0
    )

    // Clear current recommendation + bid + nomination after pick confirmed
    const updates = {
      picks: newPicks,
      availablePlayers: newAvailable,
      teamPicks,
      teamThreatScores: { ...state.teamThreatScores, [winner]: teamScore },
      recommendation: null,
      currentBid: null,
      currentNomination: null,
      ...(pick.teams_snapshot ? { teamsState: pick.teams_snapshot } : {}),
    }

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

  setSelectedTeam: (teamName) => set({ selectedTeam: teamName }),
}))
