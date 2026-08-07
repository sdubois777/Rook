import api from './client'

/** Whether this deployment can file GitHub issues (the form is hidden when it can't). */
export async function fetchFeedbackStatus() {
  const { data } = await api.get('/feedback/status')
  return data
}

/** File one bug report or suggestion. Resolves to { issue_number, issue_url }. */
export async function submitFeedback(payload) {
  const { data } = await api.post('/feedback', payload)
  return data
}
