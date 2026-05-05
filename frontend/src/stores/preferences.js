import { create } from 'zustand'
import {
  fetchWatchlist,
  addToWatchlist as apiAddToWatchlist,
  removeFromWatchlist as apiRemoveFromWatchlist,
  fetchStrategy,
  setStrategy as apiSetStrategy,
} from '../api/preferences'

export const usePreferencesStore = create((set, get) => ({
  watchlist: [],
  strategy: null,
  loading: false,

  loadWatchlist: async () => {
    const data = await fetchWatchlist()
    set({ watchlist: data.items })
  },

  addToWatchlist: async (playerId) => {
    const item = await apiAddToWatchlist(playerId)
    set((s) => ({ watchlist: [item, ...s.watchlist] }))
  },

  removeFromWatchlist: async (playerId) => {
    await apiRemoveFromWatchlist(playerId)
    set((s) => ({ watchlist: s.watchlist.filter((w) => w.player_id !== playerId) }))
  },

  isWatchlisted: (playerId) => {
    return get().watchlist.some((w) => w.player_id === playerId)
  },

  loadStrategy: async () => {
    const data = await fetchStrategy()
    set({ strategy: data.strategy })
  },

  setStrategy: async (strategy) => {
    await apiSetStrategy(strategy)
    set({ strategy })
  },
}))
