/**
 * Unified browser API shim.
 * Chrome uses chrome.* — Firefox uses browser.*
 * Always import from here, never use either directly.
 */
const browser = (
  typeof globalThis.browser !== 'undefined'
    ? globalThis.browser
    : globalThis.chrome
)

export default browser
