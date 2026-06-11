import { injectInterceptor, listenForFrames } from '../utils/draft_frames.js'

injectInterceptor()
listenForFrames('yahoo', parseYahooFrame)

function parseYahooFrame(data) {
  // STUB — real format TBD after frame capture
  // Do NOT invent frame structure
  try {
    JSON.parse(data)
    return null
  } catch {
    return null
  }
}
