import api from './client'
import { fetchDraftboard } from './draftboard'

export async function startDraft(teamId, draftRoomUrl, opts = {}) {
  const body = { your_team_id: teamId }
  if (draftRoomUrl) body.draft_room_url = draftRoomUrl
  // From the selected league context — lets the engine pick the snake vs
  // auction recommendation path (draft_type) and load league settings.
  if (opts.leagueId) body.league_id = opts.leagueId
  if (opts.draftType) body.draft_type = opts.draftType
  const { data } = await api.post('/draft/start', body)
  return data
}

export async function getDraftState() {
  const { data } = await api.get('/draft/state')
  return data
}

export async function getRecommendation() {
  const { data } = await api.get('/draft/recommendation')
  return data
}

export async function placeBid(amount) {
  const { data } = await api.post('/draft/bid', { amount })
  return data
}

export async function passNomination() {
  const { data } = await api.post('/draft/pass')
  return data
}

export async function nominatePlayer(yahooPlayerId, openingBid = 1) {
  const { data } = await api.post('/draft/nominate', {
    yahoo_player_id: yahooPlayerId,
    opening_bid: openingBid,
  })
  return data
}

export async function endDraft() {
  const { data } = await api.post('/draft/end')
  return data
}

export async function getAvailablePlayers() {
  return fetchDraftboard()
}

export async function getOpponentBudgets() {
  const { data } = await api.get('/draft/opponents')
  return data
}
