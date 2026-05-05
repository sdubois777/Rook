import { create } from 'zustand'

export const useAssistantStore = create((set, get) => ({
  // Panel state
  isOpen: false,
  isStreaming: false,

  // Context toggles
  includeRoster: true,
  includeOpponents: false,

  // Conversation (cleared on page refresh)
  messages: [], // [{role: 'user'|'assistant', content: string}]
  currentStreamText: '',

  // Pre-filled context (e.g. from "Ask about this player" button)
  prefilledPlayerIds: [],
  prefilledContext: '', // optional prepended question text

  // Actions
  open: () => set({ isOpen: true }),
  close: () => set({ isOpen: false }),
  toggle: () => set((s) => ({ isOpen: !s.isOpen })),

  setIncludeRoster: (val) => set({ includeRoster: val }),
  setIncludeOpponents: (val) => set({ includeOpponents: val }),

  addUserMessage: (content) =>
    set((s) => ({
      messages: [...s.messages, { role: 'user', content }],
    })),

  appendStreamText: (text) =>
    set((s) => ({ currentStreamText: s.currentStreamText + text })),

  finalizeAssistantMessage: () =>
    set((s) => ({
      messages: [...s.messages, { role: 'assistant', content: s.currentStreamText }],
      currentStreamText: '',
      isStreaming: false,
    })),

  setStreaming: (val) => set({ isStreaming: val }),

  clearConversation: () => set({ messages: [], currentStreamText: '' }),

  // Set prefilled context from external trigger (e.g. player detail panel)
  prefillForPlayer: (playerIds, contextText) =>
    set({
      prefilledPlayerIds: playerIds,
      prefilledContext: contextText,
      isOpen: true,
    }),

  clearPrefill: () => set({ prefilledPlayerIds: [], prefilledContext: '' }),
}))
