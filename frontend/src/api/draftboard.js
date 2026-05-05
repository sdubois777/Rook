import api from './client'

export async function fetchDraftboard(params = {}) {
  const { data } = await api.get('/draftboard', { params })
  return data
}
