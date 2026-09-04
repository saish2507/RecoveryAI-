/**
 * One WebSocket for the whole app, with reconnection backoff.
 *
 * Two decisions worth stating:
 *
 * **One connection, shared via context.** The previous build opened a socket per
 * component that wanted live data, which duplicated every message and let the
 * copies drift apart.
 *
 * **Messages invalidate the cache; they do not carry state.** A push says
 * "something about cases changed", and TanStack Query re-fetches through the
 * REST API. So the socket can never disagree with the API, and a missed message
 * during a reconnect self-heals on the next one instead of leaving the console
 * showing a case that no longer exists.
 */

import { useQueryClient } from '@tanstack/react-query'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { keys } from './api'
import type { WsMessage } from './types'

export type WsState = 'connecting' | 'open' | 'closed'

interface WsContextValue {
  state: WsState
  lastMessage: WsMessage | null
  /** Monotonic count of pushes received — a cheap "something happened" signal. */
  messageCount: number
}

const WsContext = createContext<WsContextValue>({
  state: 'closed',
  lastMessage: null,
  messageCount: 0,
})

const BASE_DELAY_MS = 500
const MAX_DELAY_MS = 15_000

function socketUrl(): string {
  const configured = import.meta.env.VITE_WS_URL
  if (configured) return configured
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws`
}

export function WsProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [state, setState] = useState<WsState>('connecting')
  const [lastMessage, setLastMessage] = useState<WsMessage | null>(null)
  const [messageCount, setMessageCount] = useState(0)

  const socketRef = useRef<WebSocket | null>(null)
  const attemptRef = useRef(0)
  const timerRef = useRef<number | null>(null)
  const closedByUsRef = useRef(false)

  const handleMessage = useCallback(
    (message: WsMessage) => {
      setLastMessage(message)
      setMessageCount((count) => count + 1)

      // Targeted invalidation: a case detail view open on an unrelated case
      // should not re-fetch because some other case moved.
      void queryClient.invalidateQueries({ queryKey: ['cases'] })
      void queryClient.invalidateQueries({ queryKey: ['review'] })
      void queryClient.invalidateQueries({ queryKey: keys.metrics() })

      const caseId = message.payload?.case_id
      if (typeof caseId === 'string') {
        void queryClient.invalidateQueries({ queryKey: keys.case(caseId) })
      }
    },
    [queryClient],
  )

  useEffect(() => {
    closedByUsRef.current = false

    const connect = () => {
      setState('connecting')
      let socket: WebSocket
      try {
        socket = new WebSocket(socketUrl())
      } catch {
        scheduleReconnect()
        return
      }
      socketRef.current = socket

      socket.onopen = () => {
        attemptRef.current = 0 // a successful connection resets the backoff
        setState('open')
      }

      socket.onmessage = (event) => {
        try {
          handleMessage(JSON.parse(event.data as string) as WsMessage)
        } catch {
          /* a malformed frame is not worth tearing the connection down for */
        }
      }

      socket.onerror = () => socket.close()

      socket.onclose = () => {
        setState('closed')
        if (!closedByUsRef.current) scheduleReconnect()
      }
    }

    const scheduleReconnect = () => {
      // Exponential backoff with jitter: without the jitter, every console
      // reconnects in lockstep and stampedes a server that just restarted.
      const attempt = attemptRef.current++
      const delay = Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS)
      const jittered = delay * (0.7 + Math.random() * 0.6)
      timerRef.current = window.setTimeout(connect, jittered)
    }

    connect()

    return () => {
      closedByUsRef.current = true
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      socketRef.current?.close()
    }
  }, [handleMessage])

  const value = useMemo(
    () => ({ state, lastMessage, messageCount }),
    [state, lastMessage, messageCount],
  )

  return <WsContext.Provider value={value}>{children}</WsContext.Provider>
}

export function useWs(): WsContextValue {
  return useContext(WsContext)
}
