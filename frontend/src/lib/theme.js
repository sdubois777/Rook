/**
 * Dark mode theme constants — blue accent.
 * Draft date: September 5, 2026.
 */

export const DRAFT_DATE = new Date('2026-09-05T12:00:00')

export const colors = {
  // Background layers
  bg: {
    primary: '#0f1117',
    secondary: '#161822',
    tertiary: '#1c1f2e',
    card: '#1a1d2b',
    hover: '#222539',
  },

  // Text
  text: {
    primary: '#e2e8f0',
    secondary: '#94a3b8',
    muted: '#64748b',
    accent: '#60a5fa',
  },

  // Accent — blue
  accent: {
    DEFAULT: '#3b82f6',
    light: '#60a5fa',
    dark: '#2563eb',
    bg: 'rgba(59, 130, 246, 0.15)',
    border: 'rgba(59, 130, 246, 0.4)',
  },

  // Borders
  border: {
    DEFAULT: '#2d3148',
    light: '#3d4260',
  },

  // Position colors
  position: {
    QB: '#a78bfa',
    RB: '#34d399',
    WR: '#60a5fa',
    TE: '#fb923c',
  },

  // System grade colors
  grade: {
    A: '#34d399',
    B: '#2dd4bf',
    C: '#fbbf24',
    D: '#fb923c',
    F: '#f87171',
  },

  // Value gap
  value: {
    undervalued: '#34d399',
    overvalued: '#f87171',
    aligned: '#94a3b8',
  },

  // Strategy highlights
  strategy: {
    primary: '#3b82f6',
    secondary: '#8b5cf6',
    dimmed: 'rgba(148, 163, 184, 0.3)',
  },
}

/**
 * Get the position color for a given position string.
 */
export function getPositionColor(pos) {
  return colors.position[pos] || colors.text.secondary
}

/**
 * Get the grade color for a letter grade (A+, A, A-, B+, etc.)
 */
export function getGradeColor(grade) {
  if (!grade) return colors.text.muted
  const letter = grade.charAt(0).toUpperCase()
  return colors.grade[letter] || colors.text.muted
}
