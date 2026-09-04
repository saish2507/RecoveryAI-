/** Presentation helpers. The vocabulary the console speaks to a human. */

import type { ActionStatus, CaseStatus, DecisionSource, Vertical } from './types'

export function currency(amount: number, code = 'INR'): string {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: code,
    maximumFractionDigits: 0,
  }).format(amount)
}

export function compactCurrency(amount: number, code = 'INR'): string {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: code,
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(amount)
}

export function percent(fraction: number, digits = 0): string {
  return `${(fraction * 100).toFixed(digits)}%`
}

export function relativeTime(iso: string): string {
  const then = new Date(iso).getTime()
  const seconds = Math.round((Date.now() - then) / 1000)
  if (!Number.isFinite(seconds)) return '—'
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

export function absoluteTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'medium',
  })
}

/** `send_payment_update_link` → `Send payment update link`. */
export function humanise(identifier: string): string {
  if (!identifier) return '—'
  const spaced = identifier.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

export const VERTICAL_LABELS: Record<Vertical, string> = {
  cart: 'Checkout',
  b2b: 'Receivables',
  autopay: 'Autopay',
}

export const CASE_STATUS_LABELS: Record<CaseStatus, string> = {
  new: 'New',
  in_progress: 'In progress',
  resolved: 'Resolved',
  escalated: 'Escalated',
  abandoned: 'Closed',
}

/**
 * What each decision source means, in one sentence.
 *
 * Surfaced as tooltips because "fallback_budget" is meaningless to a reviewer
 * looking at a case, and the difference between "the model chose this" and "the
 * model was unavailable so a table chose this" is exactly what they need to
 * weigh the decision.
 */
export const DECISION_SOURCE_LABELS: Record<DecisionSource, { label: string; hint: string }> = {
  llm: {
    label: 'LLM',
    hint: 'The model weighed the case and chose this action, then it passed the guardrail check.',
  },
  llm_cached: {
    label: 'LLM (cached)',
    hint: 'An identical case was decided earlier; the cached decision was reused at no cost.',
  },
}


export const ACTION_STATUS_LABELS: Record<ActionStatus, string> = {
  executed: 'Executed',
  blocked: 'Blocked',
  scheduled: 'Scheduled',
  escalated: 'Escalated',
  error: 'Error',
  shadow_logged: 'Shadow logged',
  pending_host_execution: 'Awaiting host',
}

export type Tone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger' | 'info'

export function caseStatusTone(status: CaseStatus): Tone {
  switch (status) {
    case 'resolved':
      return 'success'
    case 'escalated':
      return 'warning'
    case 'abandoned':
      return 'neutral'
    case 'in_progress':
      return 'accent'
    default:
      return 'info'
  }
}

export function actionStatusTone(status: ActionStatus): Tone {
  switch (status) {
    case 'executed':
      return 'success'
    case 'blocked':
    case 'error':
      return 'danger'
    case 'escalated':
      return 'warning'
    case 'shadow_logged':
      return 'info'
    default:
      return 'neutral'
  }
}

export function decisionSourceTone(source: DecisionSource): Tone {
  return source === 'llm_cached' ? 'info' : 'accent'
}

/** Low confidence is the signal a reviewer should act on, so it reads loudest. */
export function confidenceTone(confidence: number): Tone {
  if (confidence >= 0.75) return 'success'
  if (confidence >= 0.5) return 'warning'
  return 'danger'
}

/**
 * Priority bands over expected recovery.
 *
 * The raw figure is what the agent sorts and schedules on; a band is what a
 * person can read at a glance. Both are shown — the label to triage by, the
 * number because a band alone hides whether the top of the queue is worth
 * ₹80,000 or ₹80.
 *
 * Thresholds mirror `core/economics.PRIORITY_BANDS`. They are duplicated rather
 * than fetched because a band is a display concern here and a scheduling
 * decision there; a round trip to render a label would couple the two for no
 * benefit, and the API always sends the figure the label is derived from.
 */
export interface PriorityBand {
  label: string
  tone: Tone
  hint: string
}

export function priorityBand(expectedRecovery: number): PriorityBand {
  if (expectedRecovery >= 25_000)
    return {
      label: 'P0',
      tone: 'danger',
      hint: 'Highest expected recovery — revisited four times as often as the standard cadence.',
    }
  if (expectedRecovery >= 5_000)
    return { label: 'P1', tone: 'warning', hint: 'Revisited twice as often as the standard cadence.' }
  if (expectedRecovery >= 1_000)
    return { label: 'P2', tone: 'info', hint: 'Worked on the standard follow-up cadence.' }
  return {
    label: 'P3',
    tone: 'neutral',
    hint: 'Little left to recover — revisited at half the standard cadence, and closed if an attempt would cost more than it returns.',
  }
}
