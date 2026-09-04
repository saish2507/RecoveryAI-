/**
 * The trace timeline is the flagship view and carries the product's central
 * claim: that a proposal the guardrail refused is *shown as refused and
 * redirected*, not quietly replaced by its outcome.
 *
 * A regression here would not crash anything — it would just make a governed
 * agent look like a lucky one. So the redirect rendering is pinned.
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { TooltipProvider } from '@/components/ui'
import { TraceTimeline } from './TraceTimeline'
import type { CaseStep } from '@/lib/types'

function step(overrides: Partial<CaseStep> = {}): CaseStep {
  return {
    id: 1,
    case_id: 'case_x',
    step_number: 1,
    decision_source: 'llm',
    intent_id: 'intent-abc',
    proposed_action: 'send_nudge',
    final_action: 'send_nudge',
    action_params: {},
    reasoning: 'Customer looks distracted rather than price-sensitive.',
    confidence: 0.82,
    guardrail_verdict: 'allowed',
    guardrail_reason: null,
    action_status: 'executed',
    action_details: '[SIMULATED] Nudge sent to cust_1 via email',
    cost: 0,
    recovered_amount: 120,
    llm_call_made: true,
    context_snapshot: { case: { amount_at_risk: 4000 } },
    created_at: new Date().toISOString(),
    ...overrides,
  }
}

function renderTimeline(steps: CaseStep[]) {
  return render(
    <TooltipProvider>
      <TraceTimeline steps={steps} currencyCode="INR" />
    </TooltipProvider>,
  )
}

describe('an allowed decision', () => {
  it('shows who decided, why, and how confident they were', () => {
    renderTimeline([step()])

    expect(screen.getByText('LLM')).toBeInTheDocument()
    expect(screen.getByText(/82% confident/)).toBeInTheDocument()
    expect(screen.getByText(/Customer looks distracted/)).toBeInTheDocument()
    expect(screen.getByText('Executed')).toBeInTheDocument()
  })

  it('marks a reused decision as distinct from a fresh one', () => {
    renderTimeline([step({ decision_source: 'llm_cached', reasoning: 'Identical case seen earlier.' })])

    expect(screen.getByText('LLM (cached)')).toBeInTheDocument()
    expect(screen.getByText(/Agent reasoning/i)).toBeInTheDocument()
  })

  it('does not invent a guardrail block that did not happen', () => {
    renderTimeline([step()])
    expect(screen.queryByText(/Guardrail blocked it/)).not.toBeInTheDocument()
  })
})

describe('a blocked decision', () => {
  const blocked = step({
    proposed_action: 'send_discount',
    final_action: 'send_nudge',
    guardrail_verdict: 'blocked',
    guardrail_reason: 'guardrail: max_1_discount_per_90d',
    reasoning: 'High-LTV customer well above their usual spend.',
  })

  it('tells the whole story: proposed, refused, redirected', () => {
    renderTimeline([blocked])

    expect(screen.getByText('Send discount')).toBeInTheDocument()
    expect(screen.getByText(/Guardrail blocked it/)).toBeInTheDocument()
    expect(screen.getByText('guardrail: max_1_discount_per_90d')).toBeInTheDocument()
    expect(screen.getByText('Send nudge')).toBeInTheDocument()
  })

  it('keeps the refused proposal visible rather than replacing it', () => {
    renderTimeline([blocked])

    // Struck through, not removed — a reviewer must be able to see what the
    // agent wanted to do, which is the entire value of the audit trail.
    const proposal = screen.getByText('Send discount')
    expect(proposal.className).toMatch(/line-through/)
  })

  it('still shows the reasoning behind the refused proposal', () => {
    renderTimeline([blocked])
    expect(screen.getByText(/High-LTV customer/)).toBeInTheDocument()
  })
})

describe('shadow mode', () => {
  it('marks a decision that was logged but never dispatched', () => {
    renderTimeline([
      step({
        action_status: 'shadow_logged',
        action_details: "[SHADOW] would have run 'send_discount'; nothing was dispatched",
        recovered_amount: 0,
      }),
    ])

    expect(screen.getByText('Shadow logged')).toBeInTheDocument()
    expect(screen.getByText(/nothing was dispatched/)).toBeInTheDocument()
    // No revenue is claimed for a decision that never happened.
    expect(screen.queryByText(/Recovered/)).not.toBeInTheDocument()
  })
})

describe('multi-step lifecycle', () => {
  it('renders every step in order', () => {
    renderTimeline([
      step({ id: 1, step_number: 1 }),
      step({ id: 2, step_number: 2 }),
      step({ id: 3, step_number: 3 }),
    ])

    expect(screen.getByText('Step 1')).toBeInTheDocument()
    expect(screen.getByText('Step 2')).toBeInTheDocument()
    expect(screen.getByText('Step 3')).toBeInTheDocument()
  })

  it('keeps the audit payload collapsed until asked for', () => {
    renderTimeline([step()])

    expect(screen.getByText(/Show decision context/)).toBeInTheDocument()
    expect(screen.queryByText(/Context snapshot/)).not.toBeInTheDocument()
  })
})
