import { injectInterceptor, listenForFrames } from '../utils/draft_frames.js'

injectInterceptor()
listenForFrames('sleeper', parseSleeperFrame)

function parseSleeperFrame(data) {
  // STUB — real format TBD after frame capture
  try {
    JSON.parse(data)
    return null
  } catch {
    return null
  }
}
