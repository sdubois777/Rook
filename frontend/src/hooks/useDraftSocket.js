import { useEffect, useRef } from 'react'
import { useDraftStore } from '../stores/draft'

const MAX_RECONNECT_DELAY = 10000
// Backend WS endpoint is /draft/ws/draft (prefix="/draft", path="/ws/draft")
// In dev, connect directly to backend. In prod, use relative path.
const WS_PATH = '/draft/ws/draft'

function getWsUrl() {
  if (import.meta.env.DEV) {
    return `ws://localhost:8000${WS_PATH}`
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}${WS_PATH}`
}

export default function useDraftSocket() {
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)
  const reconnectDelay = useRef(1000)

  const setRecommendation = useDraftStore((s) => s.setRecommendation)
  const updateBid = useDraftStore((s) => s.updateBid)
  const recordPick = useDraftStore((s) => s.recordPick)
  const addComboAlert = useDraftStore((s) => s.addComboAlert)
  const setBridgeStatus = useDraftStore((s) => s.setBridgeStatus)
  const setWsStatus = useDraftStore((s) => s.setWsStatus)

  useEffect(() => {
    function connect() {
      const ws = new WebSocket(getWsUrl())
      wsRef.current = ws

      ws.onopen = () => {
        setWsStatus('connected')
        reconnectDelay.current = 1000
      }

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          switch (data.type) {
            case 'recommendation':
              setRecommendation(data)
              break
            case 'bid_update':
              updateBid(data)
              break
            case 'draft_pick':
              recordPick(data)
              break
            case 'opponent_combo_alert':
              addComboAlert(data)
              break
            case 'bridge_status':
              setBridgeStatus(data)
              break
          }
        } catch {
          // Ignore malformed messages
        }
      }

      ws.onclose = () => {
        setWsStatus('reconnecting')
        wsRef.current = null
        scheduleReconnect()
      }

      ws.onerror = () => {
        // onclose fires after onerror — reconnect handled there
      }
    }

    function scheduleReconnect() {
      if (reconnectTimer.current) return
      reconnectTimer.current = setTimeout(() => {
        reconnectTimer.current = null
        reconnectDelay.current = Math.min(
          reconnectDelay.current * 2,
          MAX_RECONNECT_DELAY
        )
        connect()
      }, reconnectDelay.current)
    }

    connect()

    return () => {
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current)
        reconnectTimer.current = null
      }
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
      setWsStatus('disconnected')
    }
  }, [
    setRecommendation,
    updateBid,
    recordPick,
    addComboAlert,
    setBridgeStatus,
    setWsStatus,
  ])
}
