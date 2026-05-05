import { useState, useRef, useEffect } from 'react'
import { X, Send, Trash2, Users, UserCheck } from 'lucide-react'
import Markdown from 'react-markdown'
import { useAssistantStore } from '../stores/assistant'
import { useUIStore } from '../stores/ui'
import { streamChat } from '../api/assistant'

export default function AssistantPanel() {
  const {
    isOpen,
    close,
    messages,
    currentStreamText,
    isStreaming,
    includeRoster,
    includeOpponents,
    setIncludeRoster,
    setIncludeOpponents,
    addUserMessage,
    appendStreamText,
    finalizeAssistantMessage,
    setStreaming,
    clearConversation,
    prefilledPlayerIds,
    prefilledContext,
    clearPrefill,
  } = useAssistantStore()

  const openPlayerDetail = useUIStore((s) => s.openPlayerDetail)
  const [input, setInput] = useState('')
  const messagesEndRef = useRef(null)
  const inputRef = useRef(null)

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, currentStreamText])

  // Focus input when panel opens
  useEffect(() => {
    if (isOpen) {
      setTimeout(() => inputRef.current?.focus(), 100)
    }
  }, [isOpen])

  // Apply prefilled context
  useEffect(() => {
    if (prefilledContext && isOpen) {
      setInput(prefilledContext)
      clearPrefill()
    }
  }, [prefilledContext, isOpen, clearPrefill])

  if (!isOpen) return null

  const handleSend = async () => {
    const text = input.trim()
    if (!text || isStreaming) return

    setInput('')
    addUserMessage(text)
    setStreaming(true)

    try {
      const response = await streamChat({
        message: text,
        contextType: detectContextType(text),
        playerIds: prefilledPlayerIds,
        includeRoster,
        includeOpponents,
        conversationHistory: messages,
      })

      const reader = response.body.getReader()
      const decoder = new TextDecoder()

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        const chunk = decoder.decode(value, { stream: true })
        const lines = chunk.split('\n')

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6)
            if (data === '[DONE]') break
            try {
              const parsed = JSON.parse(data)
              if (parsed.text) {
                appendStreamText(parsed.text)
              } else if (parsed.error) {
                appendStreamText(`\n\n*Error: ${parsed.error}*`)
              }
            } catch {
              // Skip malformed JSON
            }
          }
        }
      }
    } catch (err) {
      appendStreamText(`\n\n*Error: ${err.message}*`)
    }

    finalizeAssistantMessage()
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <>
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black/30 z-50" onClick={close} />

      {/* Panel */}
      <div className="fixed top-0 right-0 h-full w-[480px] max-w-[90vw] bg-[#161822] border-l border-[#2d3148] z-50 flex flex-col shadow-2xl animate-slide-in">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-[#2d3148]">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-blue-500 animate-pulse" />
            <h2 className="text-sm font-semibold text-white">AI Assistant</h2>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={clearConversation}
              className="p-1.5 rounded text-gray-400 hover:text-white hover:bg-[#2d3148] transition-colors"
              title="Clear conversation"
            >
              <Trash2 size={14} />
            </button>
            <button
              onClick={close}
              className="p-1.5 rounded text-gray-400 hover:text-white hover:bg-[#2d3148] transition-colors"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        {/* Context toggles */}
        <div className="flex items-center gap-3 px-4 py-2 border-b border-[#2d3148] bg-[#1a1d2e]">
          <span className="text-xs text-gray-500">Context:</span>
          <button
            onClick={() => setIncludeRoster(!includeRoster)}
            className={`flex items-center gap-1 px-2 py-1 rounded text-xs transition-colors ${
              includeRoster
                ? 'bg-blue-500/20 text-blue-400 border border-blue-500/30'
                : 'bg-[#2d3148] text-gray-400 border border-transparent'
            }`}
          >
            <UserCheck size={12} />
            Roster
          </button>
          <button
            onClick={() => setIncludeOpponents(!includeOpponents)}
            className={`flex items-center gap-1 px-2 py-1 rounded text-xs transition-colors ${
              includeOpponents
                ? 'bg-blue-500/20 text-blue-400 border border-blue-500/30'
                : 'bg-[#2d3148] text-gray-400 border border-transparent'
            }`}
          >
            <Users size={12} />
            Opponents
          </button>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-4 py-3 space-y-4">
          {messages.length === 0 && !currentStreamText && (
            <div className="text-center text-gray-500 text-sm mt-12">
              <p className="mb-2">Ask about players, trades, draft strategy, or lineup decisions.</p>
              <p className="text-xs text-gray-600">
                The assistant has full access to your draft bible data.
              </p>
            </div>
          )}

          {messages.map((msg, i) => (
            <MessageBubble
              key={i}
              role={msg.role}
              content={msg.content}
              openPlayerDetail={openPlayerDetail}
            />
          ))}

          {/* Streaming message */}
          {currentStreamText && (
            <MessageBubble
              role="assistant"
              content={currentStreamText}
              openPlayerDetail={openPlayerDetail}
              streaming
            />
          )}

          {isStreaming && !currentStreamText && (
            <div className="flex items-center gap-2 text-gray-400 text-sm">
              <div className="flex gap-1">
                <span className="w-1.5 h-1.5 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                <span className="w-1.5 h-1.5 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                <span className="w-1.5 h-1.5 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
              Thinking...
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="border-t border-[#2d3148] px-4 py-3">
          <div className="flex items-end gap-2">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask about a player, trade, or strategy..."
              rows={1}
              className="flex-1 resize-none bg-[#1a1d2e] border border-[#2d3148] rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-blue-500/50 max-h-24 overflow-y-auto"
              disabled={isStreaming}
            />
            <button
              onClick={handleSend}
              disabled={!input.trim() || isStreaming}
              className="p-2 rounded-lg bg-blue-600 text-white hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <Send size={16} />
            </button>
          </div>
        </div>
      </div>
    </>
  )
}

function MessageBubble({ role, content, openPlayerDetail, streaming }) {
  if (role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] bg-blue-600/20 border border-blue-500/20 rounded-lg px-3 py-2 text-sm text-gray-200">
          {content}
        </div>
      </div>
    )
  }

  return (
    <div className="max-w-[95%]">
      <div className="bg-[#1a1d2e] border border-[#2d3148] rounded-lg px-3 py-2 text-sm text-gray-200 prose prose-invert prose-sm max-w-none">
        <Markdown>{content}</Markdown>
        {streaming && <span className="inline-block w-1.5 h-4 bg-blue-400 animate-pulse ml-0.5" />}
      </div>
    </div>
  )
}

/**
 * Detect context type from message content.
 */
function detectContextType(message) {
  const lower = message.toLowerCase()
  if (lower.includes('trade') || lower.includes('offer') || lower.includes('swap')) return 'trade'
  if (lower.includes('start') || lower.includes('lineup') || lower.includes('sit')) return 'lineup'
  if (lower.includes('draft') || lower.includes('auction') || lower.includes('bid') || lower.includes('target')) return 'draft'
  if (lower.includes('waiver') || lower.includes('pickup') || lower.includes('drop')) return 'lineup'
  return 'general'
}
