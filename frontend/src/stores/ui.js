import { create } from 'zustand'

export const useUIStore = create((set) => ({
  sidebarCollapsed: false,
  selectedPlayerId: null,
  detailPanelOpen: false,
  // Bug report / suggestion dialog. Lives here, not in the sidebar, so it can be
  // opened from the full-screen draft room too — which renders no sidebar.
  feedbackOpen: false,

  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),

  openPlayerDetail: (playerId) =>
    set({ selectedPlayerId: playerId, detailPanelOpen: true }),

  closePlayerDetail: () =>
    set({ selectedPlayerId: null, detailPanelOpen: false }),

  openFeedback: () => set({ feedbackOpen: true }),
  closeFeedback: () => set({ feedbackOpen: false }),
}))
