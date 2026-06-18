import { useEffect, useRef } from 'react'
import { useDraftStore, parseClockSeconds } from '../stores/draft'
import { getRecommendation } from '../api/draft'

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

// The engine broadcasts a recommendation over the WS after a nomination, but
// that push can be missed (Sonnet latency, a reconnect). Poll as a fallback.
const REC_POLL_DELAYS = [2500, 5000, 8000]

export default function useDraftSocket() {
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)
  const reconnectDelay = useRef(1000)
  const recTimers = useRef([])

  const setRecommendation = useDraftStore((s) => s.setRecommendation)
  const setNomination = useDraftStore((s) => s.setNomination)
  const updateBid = useDraftStore((s) => s.updateBid)
  const updateClock = useDraftStore((s) => s.updateClock)
  const updateTeams = useDraftStore((s) => s.updateTeams)
  const recordPick = useDraftStore((s) => s.recordPick)
  const addComboAlert = useDraftStore((s) => s.addComboAlert)
  const setBridgeStatus = useDraftStore((s) => s.setBridgeStatus)
  const setWsStatus = useDraftStore((s) => s.setWsStatus)
  const setIsYourTurn = useDraftStore((s) => s.setIsYourTurn)
  const setCurrentPick = useDraftStore((s) => s.setCurrentPick)
  const setCurrentRound = useDraftStore((s) => s.setCurrentRound)
  const setPicksUntilYourTurn = useDraftStore((s) => s.setPicksUntilYourTurn)
  const recordSnakePick = useDraftStore((s) => s.recordSnakePick)

  useEffect(() => {
    // Recover any recommendation the engine generated before this client's
    // WebSocket connected (the broadcast would otherwise have been missed).
    // Only apply it if the store has nothing newer, so a fresh WS update or
    // a just-cleared nomination is never clobbered by the stale poll result.
    async function pollLastRecommendation() {
      try {
        const data = await getRecommendation()
        if (
          data?.type === 'recommendation' &&
          !useDraftStore.getState().recommendation
        ) {
          setRecommendation(data)
        }
      } catch {
        // No engine yet (409) or network error — nothing to recover
      }
    }
    pollLastRecommendation()

    function clearRecTimers() {
      recTimers.current.forEach(clearTimeout)
      recTimers.current = []
    }

    // After a nomination, fetch the engine's recommendation a few times until
    // it arrives — but stop once we have one, or once the nominee changes.
    function scheduleRecommendationPolls(playerName) {
      clearRecTimers()
      for (const delay of REC_POLL_DELAYS) {
        const timer = setTimeout(async () => {
          const st = useDraftStore.getState()
          if (st.recommendation) return
          if (st.currentNomination?.playerName !== playerName) return
          try {
            const rec = await getRecommendation()
            if (
              rec?.type === 'recommendation' &&
              !useDraftStore.getState().recommendation &&
              useDraftStore.getState().currentNomination?.playerName === playerName
            ) {
              setRecommendation(rec)
            }
          } catch {
            // engine not ready / network — a later poll may succeed
          }
        }, delay)
        recTimers.current.push(timer)
      }
    }

    function connect() {
      const ws = new WebSocket(getWsUrl())
      wsRef.current = ws

      ws.onopen = () => {
        setWsStatus('connected')
        reconnectDelay.current = 1000
        // Chrome suspends WebSockets when the tab/window loses focus, so a
        // reconnect may have missed events. Resync the latest recommendation
        // from the backend (no-op if the engine has nothing).
        getRecommendation()
          .then((rec) => {
            if (rec?.type === 'recommendation') setRecommendation(rec)
          })
          .catch(() => {})
      }

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          switch (data.type) {
            // Engine broadcasts (flat shape)
            case 'recommendation':
              setRecommendation(data)
              break
            case 'opponent_combo_alert':
              addComboAlert(data)
              break
            case 'bridge_status':
              setBridgeStatus(data)
              break
            // Extension relay events (nested under data.payload)
            case 'nomination': {
              const payload = data.payload || {}
              const currentNom = useDraftStore.getState().currentNomination
              // Yahoo sometimes re-fires a nomination for the SAME player when
              // the clock resets after bidding. Treat that as a clock refresh,
              // not a new nomination — otherwise the bid snaps back to $1.
              if (
                currentNom?.playerName &&
                currentNom.playerName === payload.player_name
              ) {
                if (payload.clock) {
                  updateClock({
                    clock: payload.clock,
                    seconds_remaining: parseClockSeconds(payload.clock),
                  })
                }
                break
              }
              // Genuinely new nominee: setNomination clears the stale
              // recommendation ("Analyzing..."); poll until a fresh one lands.
              // The available list is loaded once by startDraft and maintained
              // by recordPick filtering — do NOT refetch the draftboard here.
              setNomination(payload)
              scheduleRecommendationPolls(payload.player_name)
              break
            }
            case 'bid_update':
              updateBid(data.payload)
              break
            case 'clock':
              updateClock(data.payload)
              break
            case 'draft_pick':
              recordPick(data.payload)
              break
            case 'teams_update':
              updateTeams(data.payload?.teams)
              break
            // Snake draft (extension relay events, nested under data.payload)
            case 'your_turn': {
              const payload = data.payload || {}
              setIsYourTurn(true)
              setCurrentRound(payload.round ?? null)
              setCurrentPick(payload.pick ?? null)
              setPicksUntilYourTurn(0)
              // Clear the stale rec; the best-available rec arrives separately.
              setRecommendation(null)
              break
            }
            case 'your_turn_soon':
              setPicksUntilYourTurn(
                data.payload?.picks_until_your_turn ?? null
              )
              if (data.payload?.round != null) setCurrentRound(data.payload.round)
              break
            case 'snake_pick':
              recordSnakePick(data.payload || {})
              // Any pick (yours or otherwise) ends the on-the-clock state until
              // the next your_turn event arrives.
              setIsYourTurn(false)
              setPicksUntilYourTurn(null)
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
      clearRecTimers()
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
    setNomination,
    updateBid,
    updateClock,
    updateTeams,
    recordPick,
    addComboAlert,
    setBridgeStatus,
    setWsStatus,
    setIsYourTurn,
    setCurrentPick,
    setCurrentRound,
    setPicksUntilYourTurn,
    recordSnakePick,
  ])
}
