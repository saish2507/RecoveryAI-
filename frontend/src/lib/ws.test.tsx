/**
 * WebSocket provider: connection lifecycle, reconnect backoff, cache invalidation.
 *
 * This is the only genuinely stateful logic in the console, and the failure mode
 * is quiet — a socket that dies and never comes back leaves the operator staring
 * at a stale queue that looks fine. So the reconnect path is tested directly
 * rather than trusted.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { WsProvider, useWs } from './ws'

// ── A controllable WebSocket stand-in ────────────────────────────

class MockWebSocket {
  static instances: MockWebSocket[] = []
  static get last(): MockWebSocket {
    return MockWebSocket.instances[MockWebSocket.instances.length - 1]
  }

  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  closed = false
  // A plain field, not a parameter property: the build runs with
  // `erasableSyntaxOnly`, which forbids syntax that emits runtime code.
  url: string

  constructor(url: string) {
    this.url = url
    MockWebSocket.instances.push(this)
  }

  open() {
    this.onopen?.()
  }

  receive(payload: unknown) {
    this.onmessage?.({ data: typeof payload === 'string' ? payload : JSON.stringify(payload) })
  }

  /** Simulate the server or network dropping the connection. */
  drop() {
    this.closed = true
    this.onclose?.()
  }

  close() {
    this.closed = true
  }
}

function Probe() {
  const { state, messageCount, lastMessage } = useWs()
  return (
    <div>
      <span data-testid="state">{state}</span>
      <span data-testid="count">{messageCount}</span>
      <span data-testid="last">{lastMessage?.type ?? 'none'}</span>
    </div>
  )
}

function renderProvider() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  const view = render(
    <QueryClientProvider client={queryClient}>
      <WsProvider>
        <Probe />
      </WsProvider>
    </QueryClientProvider>,
  )
  return { ...view, queryClient, invalidate }
}

beforeEach(() => {
  MockWebSocket.instances = []
  vi.stubGlobal('WebSocket', MockWebSocket as unknown as typeof WebSocket)
  vi.useFakeTimers()
  // Freeze jitter so backoff delays are deterministic. The jitter itself is
  // asserted separately.
  vi.spyOn(Math, 'random').mockReturnValue(0.5)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ── Connection lifecycle ─────────────────────────────────────────

describe('connection lifecycle', () => {
  it('opens a socket on mount and reports the state', () => {
    renderProvider()

    expect(MockWebSocket.instances).toHaveLength(1)
    expect(screen.getByTestId('state')).toHaveTextContent('connecting')

    act(() => MockWebSocket.last.open())
    expect(screen.getByTestId('state')).toHaveTextContent('open')
  })

  it('targets the current host so it works behind the nginx proxy', () => {
    renderProvider()
    expect(MockWebSocket.last.url).toMatch(/^wss?:\/\/[^/]+\/ws$/)
  })

  it('reports closed when the connection drops', () => {
    renderProvider()
    act(() => MockWebSocket.last.open())

    act(() => MockWebSocket.last.drop())
    expect(screen.getByTestId('state')).toHaveTextContent('closed')
  })

  it('stops reconnecting once unmounted', () => {
    const { unmount } = renderProvider()
    act(() => MockWebSocket.last.open())

    unmount()
    act(() => MockWebSocket.last.drop())
    act(() => vi.advanceTimersByTime(60_000))

    // A provider that keeps dialling after unmount leaks a socket per navigation.
    expect(MockWebSocket.instances).toHaveLength(1)
  })
})

// ── Reconnect backoff ────────────────────────────────────────────

describe('reconnect backoff', () => {
  it('reconnects after a drop', () => {
    renderProvider()
    act(() => MockWebSocket.last.open())
    act(() => MockWebSocket.last.drop())

    expect(MockWebSocket.instances).toHaveLength(1)
    act(() => vi.advanceTimersByTime(500))
    expect(MockWebSocket.instances).toHaveLength(2)
  })

  it('backs off exponentially across repeated failures', () => {
    renderProvider()
    const delays: number[] = []

    // Drop without ever opening, so the attempt counter keeps climbing.
    for (let attempt = 0; attempt < 4; attempt++) {
      const before = MockWebSocket.instances.length
      act(() => MockWebSocket.last.drop())

      // Walk the clock forward until the next socket appears.
      let waited = 0
      while (MockWebSocket.instances.length === before && waited < 60_000) {
        act(() => vi.advanceTimersByTime(50))
        waited += 50
      }
      delays.push(waited)
    }

    // Each wait is longer than the one before it — that is the whole point.
    for (let i = 1; i < delays.length; i++) {
      expect(delays[i]).toBeGreaterThan(delays[i - 1])
    }
  })

  it('caps the delay so a long outage still retries reasonably often', () => {
    renderProvider()

    for (let attempt = 0; attempt < 12; attempt++) {
      act(() => MockWebSocket.last.drop())
      act(() => vi.advanceTimersByTime(30_000))
    }

    const before = MockWebSocket.instances.length
    act(() => MockWebSocket.last.drop())
    // MAX_DELAY_MS is 15s; with jitter the ceiling is 15s × 1.3.
    act(() => vi.advanceTimersByTime(19_500))
    expect(MockWebSocket.instances.length).toBeGreaterThan(before)
  })

  it('resets the backoff after a successful connection', () => {
    renderProvider()

    // Fail several times to climb the backoff ladder.
    for (let attempt = 0; attempt < 4; attempt++) {
      act(() => MockWebSocket.last.drop())
      act(() => vi.advanceTimersByTime(30_000))
    }

    // Now succeed, then drop again: the next retry must be fast again, not
    // stuck at the ceiling.
    act(() => MockWebSocket.last.open())
    act(() => MockWebSocket.last.drop())

    const before = MockWebSocket.instances.length
    act(() => vi.advanceTimersByTime(500))
    expect(MockWebSocket.instances.length).toBeGreaterThan(before)
  })

  it('applies jitter so reconnecting clients do not stampede a restarting server', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0)
    renderProvider()
    act(() => MockWebSocket.last.drop())

    // With random()=0 the multiplier is 0.7, so 500ms becomes 350ms — the
    // undithered 500ms tick must not have fired yet at 340ms.
    act(() => vi.advanceTimersByTime(340))
    expect(MockWebSocket.instances).toHaveLength(1)
    act(() => vi.advanceTimersByTime(20))
    expect(MockWebSocket.instances).toHaveLength(2)
  })
})

// ── Messages ─────────────────────────────────────────────────────

describe('messages', () => {
  it('invalidates the case, review and metrics caches on a push', () => {
    const { invalidate } = renderProvider()
    act(() => MockWebSocket.last.open())
    invalidate.mockClear()

    act(() => MockWebSocket.last.receive({ type: 'case.created', payload: { case_id: 'case_1' } }))

    const keys = invalidate.mock.calls.map((c) => JSON.stringify((c[0] as any)?.queryKey))
    expect(keys.some((k) => k?.includes('cases'))).toBe(true)
    expect(keys.some((k) => k?.includes('review'))).toBe(true)
    expect(keys.some((k) => k?.includes('metrics'))).toBe(true)
  })

  it('invalidates the specific case a message names', () => {
    const { invalidate } = renderProvider()
    act(() => MockWebSocket.last.open())
    invalidate.mockClear()

    act(() => MockWebSocket.last.receive({ type: 'case.updated', payload: { case_id: 'case_42' } }))

    const keys = invalidate.mock.calls.map((c) => JSON.stringify((c[0] as any)?.queryKey))
    expect(keys.some((k) => k?.includes('case_42'))).toBe(true)
  })

  it('does not invalidate a case query when no case id is present', () => {
    const { invalidate } = renderProvider()
    act(() => MockWebSocket.last.open())
    invalidate.mockClear()

    act(() => MockWebSocket.last.receive({ type: 'case.created', payload: {} }))

    const keys = invalidate.mock.calls.map((c) => JSON.stringify((c[0] as any)?.queryKey))
    expect(keys.some((k) => k?.startsWith('["case",'))).toBe(false)
  })

  it('exposes the last message and a running count', () => {
    renderProvider()
    act(() => MockWebSocket.last.open())

    act(() => MockWebSocket.last.receive({ type: 'case.created', payload: {} }))
    act(() => MockWebSocket.last.receive({ type: 'case.updated', payload: {} }))

    expect(screen.getByTestId('count')).toHaveTextContent('2')
    expect(screen.getByTestId('last')).toHaveTextContent('case.updated')
  })

  it('survives a malformed frame without tearing down the connection', () => {
    renderProvider()
    act(() => MockWebSocket.last.open())

    act(() => MockWebSocket.last.receive('{ not json'))

    expect(screen.getByTestId('state')).toHaveTextContent('open')
    expect(screen.getByTestId('count')).toHaveTextContent('0')
  })
})
