/**
 * Presentation helpers.
 *
 * These are pure functions and cheap to test, but they are also where a wrong
 * answer is least likely to be noticed: a tone that reads "healthy" for a case
 * the agent was unsure about quietly mis-sorts a reviewer's attention.
 */

import { describe, expect, it, vi } from 'vitest'
import {
  ACTION_STATUS_LABELS,
  CASE_STATUS_LABELS,
  DECISION_SOURCE_LABELS,
  actionStatusTone,
  caseStatusTone,
  compactCurrency,
  confidenceTone,
  currency,
  decisionSourceTone,
  humanise,
  percent,
  relativeTime,
} from './format'
import type { ActionStatus, CaseStatus, DecisionSource } from './types'

describe('currency', () => {
  it('renders whole rupees without decimal noise', () => {
    expect(currency(1499)).toContain('1,499')
    expect(currency(1499)).not.toContain('.00')
  })

  it('honours the case currency rather than assuming INR', () => {
    expect(currency(1000, 'USD')).toMatch(/\$|USD/)
  })

  it('compacts large figures for stat tiles', () => {
    // Dashboard tiles must not wrap; ₹2,90,000 does, ₹2.9L does not.
    expect(compactCurrency(290_000).length).toBeLessThan(10)
  })
})

describe('percent', () => {
  it('formats a fraction as a percentage', () => {
    expect(percent(0.25)).toBe('25%')
    expect(percent(0.256, 1)).toBe('25.6%')
  })

  it('handles the endpoints', () => {
    expect(percent(0)).toBe('0%')
    expect(percent(1)).toBe('100%')
  })
})

describe('humanise', () => {
  it('turns an action identifier into a readable label', () => {
    expect(humanise('send_payment_update_link')).toBe('Send payment update link')
    expect(humanise('escalate_to_human')).toBe('Escalate to human')
  })

  it('degrades to a dash rather than rendering an empty cell', () => {
    expect(humanise('')).toBe('—')
  })
})

describe('relativeTime', () => {
  it('reads as "just now" for a fresh timestamp', () => {
    expect(relativeTime(new Date().toISOString())).toBe('just now')
  })

  it('scales through seconds, minutes, hours and days', () => {
    const ago = (ms: number) => new Date(Date.now() - ms).toISOString()
    expect(relativeTime(ago(30_000))).toBe('30s ago')
    expect(relativeTime(ago(5 * 60_000))).toBe('5m ago')
    expect(relativeTime(ago(3 * 3_600_000))).toBe('3h ago')
    expect(relativeTime(ago(2 * 86_400_000))).toBe('2d ago')
  })

  it('reads a UTC timestamp as UTC regardless of the browser timezone', () => {
    // Regression: naive timestamps from SQLite were parsed as local time, so a
    // case created two seconds ago rendered as "6h ago" in IST. The backend now
    // always sends an offset; this asserts the client honours it.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-24T15:00:00Z'))
    expect(relativeTime('2026-08-24T15:00:00Z')).toBe('just now')
    expect(relativeTime('2026-08-24T14:59:30Z')).toBe('30s ago')
    vi.useRealTimers()
  })

  it('does not throw on an unparseable value', () => {
    expect(relativeTime('not a date')).toBe('—')
  })
})

describe('tone mapping', () => {
  it('flags low confidence loudest, because that is what needs a human', () => {
    expect(confidenceTone(0.95)).toBe('success')
    expect(confidenceTone(0.6)).toBe('warning')
    expect(confidenceTone(0.3)).toBe('danger')
  })

  it('treats a guardrail block and an execution error as equally alarming', () => {
    expect(actionStatusTone('blocked')).toBe('danger')
    expect(actionStatusTone('error')).toBe('danger')
    expect(actionStatusTone('executed')).toBe('success')
    expect(actionStatusTone('shadow_logged')).toBe('info')
  })

  it('separates a fresh model decision from a reused one', () => {
    expect(decisionSourceTone('llm')).toBe('accent')
    expect(decisionSourceTone('llm_cached')).toBe('info')
  })

  it('distinguishes each lifecycle status', () => {
    expect(caseStatusTone('resolved')).toBe('success')
    expect(caseStatusTone('escalated')).toBe('warning')
    expect(caseStatusTone('in_progress')).toBe('accent')
    expect(caseStatusTone('new')).toBe('info')
  })
})

describe('label coverage', () => {
  // A missing entry renders a raw identifier like `pending_host_execution` in
  // the UI, which is the kind of leak nobody notices until a demo.
  it('labels every case status', () => {
    const statuses: CaseStatus[] = ['new', 'in_progress', 'resolved', 'escalated', 'abandoned']
    for (const status of statuses) expect(CASE_STATUS_LABELS[status]).toBeTruthy()
  })

  it('labels every action status', () => {
    const statuses: ActionStatus[] = [
      'executed',
      'blocked',
      'scheduled',
      'escalated',
      'error',
      'shadow_logged',
      'pending_host_execution',
    ]
    for (const status of statuses) expect(ACTION_STATUS_LABELS[status]).toBeTruthy()
  })

  it('gives every decision source both a label and an explanation', () => {
    // Two, and only two. A decision is either the model's answer or the model's
    // cached answer; when it cannot answer, no step is recorded at all.
    const sources: DecisionSource[] = ['llm', 'llm_cached']
    for (const source of sources) {
      expect(DECISION_SOURCE_LABELS[source].label).toBeTruthy()
      // The hint is what makes "fallback_budget" mean something to a reviewer.
      expect(DECISION_SOURCE_LABELS[source].hint.length).toBeGreaterThan(20)
    }
  })
})
