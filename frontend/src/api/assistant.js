/**
 * AI Assistant API — streaming chat with draft bible context.
 */

const BASE_URL = '/api/assistant'

/**
 * Send a chat message and return a ReadableStream of SSE chunks.
 * Caller is responsible for consuming the stream.
 */
export async function streamChat({
  message,
  contextType = 'general',
  playerIds = [],
  includeRoster = true,
  includeOpponents = false,
  conversationHistory = [],
}) {
  const response = await fetch(`${BASE_URL}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      context_type: contextType,
      player_ids: playerIds,
      include_roster: includeRoster,
      include_opponents: includeOpponents,
      conversation_history: conversationHistory,
    }),
  })

  if (!response.ok) {
    throw new Error(`Assistant API error: ${response.status}`)
  }

  return response
}
