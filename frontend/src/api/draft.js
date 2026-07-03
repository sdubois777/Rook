import api from './client'
import { fetchDraftboard } from './draftboard'

export async function startDraft(opts = {}) {
  // No team name — the extension self-identifies your team; Rook derives the
  // label. From the selected league context: draft_type picks the snake vs
  // auction recommendation path and league_id loads league settings.
  const body = {}
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
