import api from './client'

export async function fetchNews(params = {}) {
  const { data } = await api.get('/news', { params })
  return data
}

// Distinct signal types present in the feed — powers the Type filter so its
// options are derived from real data (never a hardcoded, drift-prone list).
export async function fetchNewsTypes() {
  const { data } = await api.get('/news/types')
  return data
}
