/**
 * Mirrors of the backend's Pydantic read models.
 *
 * Hand-written rather than generated from OpenAPI: the surface is small, and a
 * generator would be one more build step between a schema change and noticing
 * it. If these drift, the console breaks loudly in TypeScript rather than
 * quietly at runtime.
 */

export type Vertical = 'cart' | 'b2b' | 'autopay'
export type LTVTier = 'low' | 'medium' | 'high'

export type CaseStatus = 'new' | 'in_progress' | 'resolved' | 'escalated' | 'abandoned'

export type DecisionSource = 'llm' | 'llm_cached'

export type ActionStatus =
  | 'executed'
  | 'blocked'
  | 'scheduled'
  | 'escalated'
  | 'error'
  | 'shadow_logged'
  | 'pending_host_execution'

export interface CaseStep {
  id: number
  case_id: string
  step_number: number
  decision_source: DecisionSource
  intent_id: string
  proposed_action: string
  final_action: string
  action_params: Record<string, unknown>
  reasoning: string
  confidence: number
  guardrail_verdict: 'allowed' | 'blocked'
  guardrail_reason: string | null
  action_status: ActionStatus
  action_details: string
  cost: number
  recovered_amount: number
  llm_call_made: boolean
  context_snapshot: Record<string, any>
  created_at: string
}

export interface Case {
  id: string
  event_id: string
  vertical: Vertical
  customer_id: string
  ltv_tier: LTVTier
  amount: number
  currency: string
  status: CaseStatus
  priority_score: number
  expected_recovery: number
  raw_failure_reason: string
  diagnosis: string
  vertical_metadata: Record<string, unknown>
  amount_recovered: number
  cost_of_recovery: number
  step_count: number
  latest_confidence: number | null
  created_at: string
  updated_at: string
  next_followup_at: string | null
}

export interface CaseDetail extends Case {
  steps: CaseStep[]
}

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface Metrics {
  cases: {
    total: number
    open: number
    by_status: Record<string, number>
    by_vertical: Record<string, number>
  }
  revenue: {
    at_risk: number
    recovered: number
    cost_of_recovery: number
    net_recovered: number
    recovery_rate: number
  }
  decisions: {
    steps: number
    by_source: Record<string, number>
    by_final_action: Record<string, number>
    guardrail_blocks: number
    llm_call_share: number
  }
}

export interface SystemStatus {
  status: string
  agent: {
    agent_mode: 'live' | 'shadow'
    executor: string
    max_case_steps: number
    followup_delay_seconds: number
    known_actions: string[]
    llm: {
      provider: string
      /** A key is present. Independent of the runtime switch below. */
      configured: boolean
      /** The runtime switch. False → every decision comes from the rule engine. */
      enabled: boolean
      /** `configured && enabled` — what the agent actually routes on. */
      available: boolean
      cache_entries: number
      minute_tokens_remaining: number
      minute_capacity: number
      day_tokens_remaining: number
      day_capacity: number
      day_calls_made?: number
      day_budget?: number
    }
  }
  scheduler: {
    running: boolean
    poll_interval_seconds: number
    runs: number
    decisions_made: number
    errors: number
    last_run_at: string | null
  }
  websocket: { connections: number }
  security: {
    api_key_auth_enabled: boolean
    webhook_signature_verification_enabled: boolean
    cors_origins: string[]
    warnings: string[]
  }
}

export interface ApiErrorBody {
  error: { code: string; message: string }
}

/** Push message from the server. Carries a hint, not state — see `WsProvider`. */
export interface WsMessage {
  type: 'case.created' | 'case.updated'
  payload: Record<string, any>
}

/** One population-level signal that changed shape recently. */
export interface Anomaly {
  kind: string
  key: string
  label: string
  recent_count: number
  recent_per_hour: number
  baseline_per_hour: number
  factor: number | null
  amount_at_risk: number
  severity: 'low' | 'medium' | 'high'
  detail: string
  sample_case_ids: string[]
}

export interface AnomalyReport {
  window_minutes: number
  baseline_hours: number
  count: number
  highest_severity: string | null
  anomalies: Anomaly[]
}
