import axios from 'axios'

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000'

const api = axios.create({
  baseURL: API_BASE,
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
})

// Token getter — registered by AuthProvider
let _getToken = null

export function registerTokenGetter(fn) {
  _getToken = fn
}

// Request interceptor — add auth token
api.interceptors.request.use(
  async (config) => {
    if (_getToken) {
      try {
        const token = await _getToken()
        if (token) {
          config.headers.Authorization = `Bearer ${token}`
        }
      } catch (e) {
        console.warn('Could not get auth token:', e)
      }
    }
    return config
  },
  (error) => Promise.reject(error),
)

// Response interceptor — handle 401
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      window.dispatchEvent(new Event('clerk:auth-error'))
    }
    return Promise.reject(error)
  },
)

export { api as apiClient }
export default api
