import { create } from 'zustand'

export const useUIStore = create((set) => ({
  sidebarCollapsed: false,
  selectedPlayerId: null,
  detailPanelOpen: false,

  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),

  openPlayerDetail: (playerId) =>
    set({ selectedPlayerId: playerId, detailPanelOpen: true }),

  closePlayerDetail: () =>
    set({ selectedPlayerId: null, detailPanelOpen: false }),
}))
