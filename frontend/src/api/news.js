import api from './client'

export async function fetchNews(params = {}) {
  const { data } = await api.get('/news', { params })
  return data
}
