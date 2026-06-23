import { create } from 'zustand'
import {
  startDraft as apiStartDraft,
  getDraftState,
  getAvailablePlayers,
  getOpponentBudgets,
  getRecommendation,
  endDraft as apiEndDraft,
} from '../api/draft'
import { normalizeName } from '../utils/names'
import { matchesPickName } from '../utils/playerUtils'

// Your team name is needed to attribute future picks after a page-refresh
// rehydrate (the backend /draft/state doesn't return your_team_id). Persist it
// at draft start so a reload can restore it without a backend change.
const TEAM_KEY = 'rook_draft_team'

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

  // Snake draft turn tracking (auction leaves these at their defaults)
  isYourTurn: false,
  currentPick: null,
  currentRound: null,
  picksUntilYourTurn: null,

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

  startDraft: async (teamId, draftRoomUrl, opts = {}) => {
    const startResp = await apiStartDraft(teamId, draftRoomUrl, opts)

    // Load initial state + available players in parallel
    const [state, board] = await Promise.all([
      getDraftState(),
      getAvailablePlayers(),
    ])

    // Flatten tiers into a single list
    const tiers = board?.tiers || {}
    const players = Object.values(tiers).flat()
    console.debug('Rook: draftboard players loaded:', players.length)

    const myTeamName = startResp?.team_name || teamId || null
    // Persist for page-refresh rehydration (see rehydrate()).
    try {
      if (myTeamName) localStorage.setItem(TEAM_KEY, myTeamName)
    } catch { /* localStorage unavailable — non-fatal */ }

    set({
      phase: 'live',
      // Team name used to detect picks you win — prefer the backend echo,
      // fall back to the id entered at setup.
      myTeamName,
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
      // Update budget from recommendation if included (rec may be null when
      // clearing, e.g. on your_turn — guard the optional chain).
      ...(rec?.budget_summary
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

  updateTeams: (teams) =>
    set((s) => {
      if (!teams || typeof teams !== 'object') return { teamsState: {} }
      // The React poller labels YOUR OWN card "You"; the engine knows you as
      // myTeamName. Fold "You" into myTeamName so the panel shows ONE entry for
      // you, not a phantom "You" beside "<your team> (you)".
      const me = s.myTeamName
      if (me && me !== 'You' && Object.prototype.hasOwnProperty.call(teams, 'You')) {
        const { You, ...rest } = teams
        return { teamsState: { ...rest, [me]: You } }
      }
      return { teamsState: teams }
    }),

  recordPick: (pick) => {
    const state = get()
    // Normalized key so punctuation/casing differences between the DOM poller
    // and the draftboard ("Amon-Ra St. Brown" vs "Amon Ra St Brown") still match.
    const pickName = normalizeName(pick.player_name)

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
          normalizeName(p.player_name) === pickName &&
          now - (p.timestamp || 0) < TWO_SECONDS
      )
    ) {
      console.debug('Rook: dedup blocked duplicate pick:', pick.player_name)
      return
    }

    // Did we win this player? The relay carries `winner` (team display name);
    // the engine path may set `is_yours`. Match winner against our team name.
    const isYours =
      pick.is_yours ||
      (state.myTeamName &&
        pick.winner &&
        normalizeName(pick.winner) === normalizeName(state.myTeamName))

    console.debug(
      'Rook: recordPick',
      pick.player_name,
      '| winner:',
      pick.winner,
      '| myTeamName:',
      state.myTeamName,
      '| isYours:',
      !!isYours,
    )

    const newPicks = [...state.picks, { ...pick, timestamp: now }]

    // Match the picked player to the available list. The backend enriches the
    // auction draft_pick with the FULL name + canonical id, but the React DOM's
    // raw name is abbreviated ("T. McMillan") — so match by id (guarded so
    // undefined!==undefined can't wipe the list) OR matchesPickName, which
    // handles abbreviated DOM names (same backstop snake uses).
    const matchesPick = (p) =>
      (pick.player_id != null &&
        (p.id === pick.player_id || p.yahoo_player_id === pick.player_id)) ||
      (!!pick.player_name && matchesPickName(p.name, pick.player_name))

    const fromAvailable = state.availablePlayers.find(matchesPick)
    const newAvailable = state.availablePlayers.filter((p) => !matchesPick(p))

    // Group the pick under its winning team and recompute that team's threat
    // score (sum of ai_bid_ceiling). Collapse YOUR wins (the React poller labels
    // your card "You", and is_yours is set when you win) into myTeamName, so the
    // roster dropdown shows ONE entry for you — not a phantom "You" beside it.
    const winner = isYours ? state.myTeamName || 'You' : pick.winner || 'Unknown'
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

  // --- Snake draft turn actions ---

  setIsYourTurn: (val) => set({ isYourTurn: val }),
  setCurrentPick: (pick) => set({ currentPick: pick }),
  setCurrentRound: (round) => set({ currentRound: round }),
  setPicksUntilYourTurn: (n) => set({ picksUntilYourTurn: n }),

  // A snake pick was made (by anyone). Remove the player from the available
  // list, track it under the winning team, and if it's ours, add to roster.
  recordSnakePick: (pick) =>
    set((state) => {
      const pickName = normalizeName(pick.player_name || '')

      // DEDUP: ignore a same-pick redelivery within 2s (double socket / retry).
      const now = Date.now()
      if (
        pickName &&
        state.picks.some(
          (p) =>
            normalizeName(p.player_name) === pickName &&
            now - (p.timestamp || 0) < 2000
        )
      ) {
        return state
      }

      const isYours =
        pick.is_yours ||
        (state.myTeamName &&
          pick.picker &&
          normalizeName(pick.picker) === normalizeName(state.myTeamName))

      // Match the picked player by UUID id (the backend enriches snake_pick with
      // the canonical id by resolving the abbreviated DOM name), then by name —
      // matchesPickName also handles abbreviated DOM names ("C. MCCAFFREY") as a
      // frontend backstop. Guard the id check so undefined === undefined can't
      // wipe the whole list.
      const matchesPick = (p) =>
        (pick.id != null && p.id === pick.id) ||
        (pick.yahoo_player_id != null && p.yahoo_player_id === pick.yahoo_player_id) ||
        (!!pick.player_name && matchesPickName(p.name, pick.player_name))

      const fromAvailable = state.availablePlayers.find(matchesPick)
      const newAvailable = state.availablePlayers.filter((p) => !matchesPick(p))

      // Collapse the Picks-panel's "You" (and any picker that resolves to us)
      // into one consistent team key, so the roster dropdown shows a single
      // entry per team instead of "You" / "Stephen" / "unknown" duplicates.
      const winner = isYours ? state.myTeamName || 'You' : pick.picker || 'Unknown'
      const teamPicks = {
        ...state.teamPicks,
        [winner]: [
          ...(state.teamPicks[winner] || []),
          {
            player_name: pick.player_name,
            pick_number: pick.pick_number,
            round: pick.round,
            position: pick.position || fromAvailable?.position || null,
          },
        ],
      }

      const updates = {
        picks: [...state.picks, { ...pick, timestamp: now }],
        availablePlayers: newAvailable,
        teamPicks,
        // Someone just picked — it's no longer our turn until the next event.
        isYourTurn: false,
        recommendation: isYours ? null : state.recommendation,
      }

      if (isYours) {
        updates.myRoster = [
          ...state.myRoster,
          {
            player_id: pick.yahoo_player_id,
            player_name: pick.player_name,
            position: pick.position || fromAvailable?.position || null,
            price: 0,
          },
        ]
        updates.rosterSlotsRemaining = state.rosterSlotsRemaining - 1
      }

      return updates
    }),

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

  // Full frontend rehydration after a PAGE REFRESH (the backend session is intact
  // via #96; the in-memory store reset to empty). Composes the existing GETs —
  // /draft/state (your roster + budget), /draft/opponents (their rosters +
  // budgets + threat), /draft/recommendation (the suggested pick) — and rebuilds
  // the available list from the draftboard pool minus everyone already drafted.
  //
  // Returns true if an active backend draft was found and the store hydrated;
  // false (no active session → /state 409) so the caller falls through to setup.
  // Idempotent: safe to call when already 'live' (it just re-syncs a snapshot).
  rehydrate: async () => {
    let state
    try {
      state = await getDraftState()
    } catch (e) {
      if (e?.response?.status === 409) {
        // No resumable draft (none, ended, or stale past the resume window).
        // Clear the leftover team id so it can't outlive the draft it belonged to.
        try {
          localStorage.removeItem(TEAM_KEY)
        } catch { /* ignore */ }
        return false
      }
      throw e
    }

    // Pull opponents + recommendation + the board pool in parallel. Each is
    // best-effort: a failure degrades gracefully rather than aborting the whole
    // rehydrate (rosters/budget from /state still come back).
    const [opponents, rec, board] = await Promise.all([
      getOpponentBudgets().catch(() => ({ opponents: {} })),
      getRecommendation().catch(() => null),
      getAvailablePlayers().catch(() => null),
    ])

    // Rebuild opponents → teamPicks / threat / budgets, and collect drafted names.
    const teamPicks = {}
    const teamThreatScores = {}
    const opponentBudgets = {}
    const draftedNames = new Set()
    for (const [teamId, info] of Object.entries(opponents?.opponents || {})) {
      teamPicks[teamId] = (info.roster || []).map((p) => ({
        player_name: p.player_name,
        position: p.position,
        price: p.price,
        ceiling: null,
      }))
      teamThreatScores[teamId] = info.threat_score || 0
      opponentBudgets[teamId] = info.budget
      for (const p of info.roster || []) draftedNames.add(normalizeName(p.player_name))
    }

    // Your roster → drafted set + your own team-picks entry (team name from
    // localStorage; /state doesn't return your_team_id).
    const myRoster = state.your_roster || []
    for (const p of myRoster) draftedNames.add(normalizeName(p.player_name))
    let myTeamName = get().myTeamName
    try {
      myTeamName = localStorage.getItem(TEAM_KEY) || myTeamName
    } catch { /* ignore */ }
    if (myTeamName && myRoster.length > 0) {
      teamPicks[myTeamName] = myRoster.map((p) => ({
        player_name: p.player_name,
        position: p.position,
        price: p.price,
        ceiling: null,
      }))
    }

    // Available = draftboard pool minus everyone drafted. Fall back to the
    // already-loaded list if the board fetch failed.
    const pool = Object.values(board?.tiers || {}).flat()
    const basePool = pool.length > 0 ? pool : get().availablePlayers
    const availablePlayers = basePool.filter(
      (p) => !draftedNames.has(normalizeName(p.name))
    )

    set({
      phase: 'live',
      myTeamName: myTeamName || get().myTeamName,
      myBudget: state.your_remaining_budget,
      myRoster,
      rosterSlotsRemaining: state.roster_slots_remaining,
      spendable: state.spendable_on_next_player,
      positionalCounts: state.positional_counts || {},
      opponentBudgets,
      teamPicks,
      teamThreatScores,
      ...(availablePlayers.length > 0 ? { availablePlayers } : {}),
    })

    // Re-fetch the CURRENT recommendation (the suggested pick) — first-class, not
    // a stale/placeholder. The backend value is computed against the live state,
    // identical to what a fresh pick would yield. If there's no active nomination
    // it returns no_recommendation and we leave the panel empty (correct).
    if (rec && rec.type === 'recommendation') {
      set({ recommendation: rec })
      // Recover the nominee card from the rec so it isn't blank; the live bid and
      // clock repopulate on the next WS bid_update/clock tick.
      if (rec.player_name) {
        set((s) => ({
          currentNomination: s.currentNomination || {
            playerName: rec.player_name,
            posTeam: null,
            currentBid: null,
            clock: null,
            secondsRemaining: null,
          },
        }))
      }
    }

    return true
  },

  endDraft: async () => {
    await apiEndDraft()
    try {
      localStorage.removeItem(TEAM_KEY)
    } catch { /* ignore */ }
    set({ phase: 'ended' })
  },

  setAvailableFilter: (filter) => {
    set((s) => ({
      availableFilter: { ...s.availableFilter, ...filter },
    }))
  },

  setSelectedTeam: (teamName) => set({ selectedTeam: teamName }),
}))
