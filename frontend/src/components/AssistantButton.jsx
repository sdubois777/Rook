import { MessageCircle } from 'lucide-react'
import { useAssistantStore } from '../stores/assistant'

export default function AssistantButton() {
  const toggle = useAssistantStore((s) => s.toggle)
  const isOpen = useAssistantStore((s) => s.isOpen)

  if (isOpen) return null

  return (
    <button
      onClick={toggle}
      className="fixed bottom-6 right-6 z-40 p-3.5 rounded-full bg-blue-600 text-white shadow-lg hover:bg-blue-500 hover:scale-105 transition-all duration-200"
      title="AI Assistant"
    >
      <MessageCircle size={22} />
    </button>
  )
}
