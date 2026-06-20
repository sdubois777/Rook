/**
 * Shared extension constants — storage keys, message types, and the
 * page-world event name, previously scattered as inline strings.
 *
 * STORAGE_KEYS.CAPTURE_MODE is the single key for the debug capture
 * toggle; the popup writes it and the draft content scripts read it.
 */

export const STORAGE_KEYS = {
  DRAFT_TOKEN: 'draft_token',
  CAPTURE_MODE: 'capture_mode',
  CAPTURED_FRAMES: 'captured_frames',
  ESPN_CONNECTED: 'espn_connected',
  ESPN_LEAGUE_ID: 'espn_league_id',
  YAHOO_SYNCED_AT: 'yahoo_synced_at',
  SLEEPER_SYNCED_AT: 'sleeper_synced_at',
  ACTIVE_DRAFT: 'active_draft',
  DRAFT_PLATFORM: 'draft_platform',
}

/** Clear the draft-active indicator after this long with no nominations. */
export const DRAFT_INACTIVITY_MS = 5 * 60 * 1000

export const MESSAGE_TYPES = {
  DRAFT_EVENT: 'DRAFT_EVENT',
  ESPN_COOKIES: 'ESPN_COOKIES',
}

/** CustomEvent dispatched by the page-world WS interceptor. */
export const WS_FRAME_EVENT = '__rook_ws_frame__'

/** How many captured frames to keep in storage (debug capture mode). */
export const MAX_CAPTURED_FRAMES = 50
