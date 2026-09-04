/** HTTP client and query hooks. Every server read in the app goes through here. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type {
  ApiErrorBody,
  Case,
  CaseDetail,
  AnomalyReport,
  Metrics,
  Page,
  SystemStatus,
  Vertical,
} from './types'

const BASE = import.meta.env.VITE_API_BASE_URL ?? ''

export class ApiError extends Error {
  // Declared as fields rather than parameter properties: the build runs with
  // `erasableSyntaxOnly`, which forbids syntax that emits runtime code.
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })

  if (!response.ok) {
    // The API guarantees one error shape, so this reads it directly rather than
    // guessing at the body — but a proxy or gateway error can still arrive as
    // HTML, hence the fallback.
    let body: ApiErrorBody | undefined
    try {
      body = (await response.json()) as ApiErrorBody
    } catch {
      /* not JSON */
    }
    throw new ApiError(
      response.status,
      body?.error?.code ?? 'unknown_error',
      body?.error?.message ?? `Request failed with HTTP ${response.status}.`,
    )
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

// ── Query keys ───────────────────────────────────────────────────
// Exported so the WebSocket provider can invalidate precisely rather than
// nuking the whole cache on every push.

export const keys = {
  cases: (filters?: Record<string, unknown>) => ['cases', filters ?? {}] as const,
  case: (id: string) => ['case', id] as const,
  review: (filters?: Record<string, unknown>) => ['review', filters ?? {}] as const,
  metrics: () => ['metrics'] as const,
  anomalies: () => ['anomalies'] as const,
  status: () => ['status'] as const,
  scenarios: () => ['scenarios'] as const,
}

// ── Reads ────────────────────────────────────────────────────────

export interface CaseFilters {
  status?: string
  vertical?: Vertical | ''
  open_only?: boolean
  limit?: number
  offset?: number
  [key: string]: string | number | boolean | undefined
}

export function useCases(filters: CaseFilters = {}) {
  return useQuery({
    queryKey: keys.cases(filters),
    queryFn: () => request<Page<Case>>(`/api/v1/cases${query({ ...filters })}`),
    placeholderData: (previous) => previous, // no flicker when filters change
  })
}

export function useCase(id: string | undefined) {
  return useQuery({
    queryKey: keys.case(id ?? ''),
    queryFn: () => request<CaseDetail>(`/api/v1/cases/${id}`),
    enabled: Boolean(id),
  })
}

export function useReviewQueue(filters: { limit?: number; offset?: number; include?: string } = {}) {
  return useQuery({
    queryKey: keys.review(filters),
    queryFn: () => request<Page<Case>>(`/api/v1/review/queue${query({ ...filters })}`),
    placeholderData: (previous) => previous,
  })
}

/**
 * Population-level signals. Polls rather than waiting on the socket: an anomaly
 * is a property of many cases over a window, so no single `case.updated` event
 * implies the picture changed, and none arriving does not imply it did not.
 */
export function useAnomalies() {
  return useQuery({
    queryKey: keys.anomalies(),
    queryFn: () => request<AnomalyReport>('/api/v1/system/anomalies'),
    refetchInterval: 60_000,
  })
}


export function useMetrics() {
  return useQuery({
    queryKey: keys.metrics(),
    queryFn: () => request<Metrics>('/api/v1/system/metrics'),
  })
}

export function useSystemStatus() {
  return useQuery({
    queryKey: keys.status(),
    queryFn: () => request<SystemStatus>('/api/v1/system/status'),
    // Rate-limit headroom moves without any case activity, so this one polls
    // rather than waiting for a WebSocket nudge.
    refetchInterval: 10_000,
  })
}

export function useScenarios() {
  return useQuery({
    queryKey: keys.scenarios(),
    queryFn: () => request<Record<string, string>>('/api/v1/dev/scenarios'),
    staleTime: Infinity, // a static catalogue
  })
}

// ── Writes ───────────────────────────────────────────────────────

function useInvalidateAll() {
  const client = useQueryClient()
  return () => {
    void client.invalidateQueries({ queryKey: ['cases'] })
    void client.invalidateQueries({ queryKey: ['review'] })
    void client.invalidateQueries({ queryKey: ['metrics'] })
  }
}

/**
 * Flips model consultation on or off. The server returns the new LLM snapshot,
 * which is written straight into the status cache so the switch settles
 * immediately instead of waiting out the 10s status poll.
 */
export function useSetLlmEnabled() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (enabled: boolean) =>
      request<SystemStatus['agent']['llm']>('/api/v1/system/llm', {
        method: 'POST',
        body: JSON.stringify({ enabled }),
      }),
    onSuccess: (llm) => {
      client.setQueryData<SystemStatus>(keys.status(), (previous) =>
        previous ? { ...previous, agent: { ...previous.agent, llm } } : previous,
      )
    },
  })
}

export function useInjectEvent() {
  const invalidate = useInvalidateAll()
  return useMutation({
    mutationFn: (params: { scenario?: string; vertical?: Vertical; customer_id?: string }) =>
      request<Case>(`/api/v1/dev/inject${query({ ...params })}`, { method: 'POST' }),
    onSuccess: invalidate,
  })
}

export function useAdvanceCase() {
  const client = useQueryClient()
  const invalidate = useInvalidateAll()
  return useMutation({
    mutationFn: (caseId: string) =>
      request<Record<string, unknown>>(`/api/v1/dev/cases/${caseId}/advance`, { method: 'POST' }),
    onSuccess: (_data, caseId) => {
      void client.invalidateQueries({ queryKey: keys.case(caseId) })
      invalidate()
    },
  })
}

export function useRunFollowups() {
  const client = useQueryClient()
  const invalidate = useInvalidateAll()
  return useMutation({
    mutationFn: () =>
      request<{ advanced: number; cases: Record<string, unknown>[] }>('/api/v1/dev/followups/run', {
        method: 'POST',
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['case'] })
      invalidate()
    },
  })
}
