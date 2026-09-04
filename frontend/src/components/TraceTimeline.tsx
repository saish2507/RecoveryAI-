/**
 * The trace timeline — the flagship view, and the whole argument for trusting
 * the agent.
 *
 * Each step tells the complete story in the order it happened: who decided
 * (rules or model), what they proposed, what the guardrail said, what actually
 * ran. When a guardrail intercepts a proposal, the timeline shows the proposal
 * *and* the redirect rather than only the outcome — "the agent wanted to send a
 * second discount and was stopped" is far more informative than "a nudge was
 * sent", and it is the only way a reviewer can tell a governed agent from a
 * lucky one.
 *
 * The agent's reasoning renders in monospace, deliberately. It marks the text as
 * raw model output rather than product copy.
 */

import { AnimatePresence, motion } from 'framer-motion'
import {
  Ban,
  Bot,
  ChevronDown,
  CornerDownRight,
  Mail,
  ShieldAlert,
  ShieldCheck,
} from 'lucide-react'
import { useState } from 'react'
import { Badge, Tooltip, cn } from '@/components/ui'
import {
  ACTION_STATUS_LABELS,
  DECISION_SOURCE_LABELS,
  absoluteTime,
  actionStatusTone,
  confidenceTone,
  currency,
  decisionSourceTone,
  humanise,
  percent,
  relativeTime,
} from '@/lib/format'
import type { CaseStep } from '@/lib/types'

/**
 * The words the customer actually receives.
 *
 * An action name says what the agent decided; it says nothing about what gets
 * sent under the company's name, which is the part that can promise the wrong
 * thing or land badly. Rendered as an email rather than as two more fields,
 * because the question a reviewer is answering here is "would I send this?" and
 * that is easier to answer when it looks like the thing being sent.
 */
function DraftedMessage({ step }: { step: CaseStep }) {
  const params = (step.action_params ?? {}) as Record<string, unknown>
  const subject = typeof params.message_subject === 'string' ? params.message_subject : ''
  const body = typeof params.message_body === 'string' ? params.message_body : ''
  if (!subject && !body) return null

  const channel = typeof params.channel === 'string' ? params.channel : 'email'

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-background">
      <div className="flex items-center gap-2 border-b border-border/70 bg-surface px-3 py-1.5">
        <Mail size={12} className="text-subtle" />
        <span className="text-[0.62rem] font-semibold uppercase tracking-[0.08em] text-subtle">
          Drafted {channel}
        </span>
      </div>
      <div className="space-y-1.5 px-3 py-2.5">
        {subject ? (
          <p className="text-[0.82rem] font-semibold text-foreground">{subject}</p>
        ) : null}
        {body ? (
          <p className="text-xs leading-relaxed whitespace-pre-line text-muted">{body}</p>
        ) : null}
      </div>
    </div>
  )
}

function SourceBadge({ step }: { step: CaseStep }) {
  const meta = DECISION_SOURCE_LABELS[step.decision_source] ?? {
    label: step.decision_source,
    hint: '',
  }
  const Icon = Bot
  return (
    <Tooltip content={meta.hint}>
      <Badge tone={decisionSourceTone(step.decision_source)} className="cursor-default">
        <Icon size={11} />
        {meta.label}
      </Badge>
    </Tooltip>
  )
}

function StepCard({ step, currencyCode }: { step: CaseStep; currencyCode: string }) {
  const [open, setOpen] = useState(false)
  const blocked = step.guardrail_verdict === 'blocked'

  return (
    <div className="timeline-rail relative pl-10">
      {/* Marker on the rail. Colour alone never carries the meaning — the icon
          differs too, so it survives a colour-blind reader. */}
      <div
        className={cn(
          'absolute left-2 top-1 flex h-6 w-6 items-center justify-center rounded-full border-2',
          blocked
            ? 'border-danger bg-danger-soft text-danger'
            : 'border-success bg-success-soft text-success',
        )}
      >
        {blocked ? <ShieldAlert size={13} /> : <ShieldCheck size={13} />}
      </div>

      <div className="rounded-card border border-border bg-surface">
        <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
          <span className="text-xs font-bold uppercase tracking-wider text-subtle">
            Step {step.step_number}
          </span>
          <SourceBadge step={step} />
          <Tooltip content="The agent's own stated confidence in this decision. It drives the review queue ordering.">
            <Badge tone={confidenceTone(step.confidence)} className="cursor-default">
              {percent(step.confidence)} confident
            </Badge>
          </Tooltip>
          <Badge tone={actionStatusTone(step.action_status)}>
            {ACTION_STATUS_LABELS[step.action_status] ?? step.action_status}
          </Badge>
          <Tooltip content={absoluteTime(step.created_at)}>
            <span className="ml-auto cursor-default text-xs text-subtle">
              {relativeTime(step.created_at)}
            </span>
          </Tooltip>
        </div>

        <div className="space-y-3 px-4 py-3">
          {/* The decision chain. This is the part that matters. */}
          <div className="space-y-1.5">
            <div className="flex items-center gap-2 text-sm">
              <span className="text-xs uppercase tracking-wide text-subtle">Proposed</span>
              <span className={cn('font-medium', blocked && 'text-muted line-through')}>
                {humanise(step.proposed_action)}
              </span>
            </div>

            {blocked ? (
              <>
                <div className="flex items-center gap-2 text-sm text-danger">
                  <Ban size={13} />
                  <span className="font-medium">Guardrail blocked it</span>
                  <code className="rounded bg-danger-soft px-1.5 py-0.5 font-mono text-[0.7rem]">
                    {step.guardrail_reason}
                  </code>
                </div>
                <div className="flex items-center gap-2 text-sm">
                  <CornerDownRight size={13} className="text-accent-hover" />
                  <span className="text-xs uppercase tracking-wide text-subtle">Redirected to</span>
                  <span className="font-semibold text-accent-hover">
                    {humanise(step.final_action)}
                  </span>
                </div>
              </>
            ) : null}
          </div>

          {step.reasoning ? (
            <div className="rounded-lg border border-border bg-background px-3 py-2.5">
              <p className="mb-1 text-[0.62rem] font-semibold uppercase tracking-[0.08em] text-subtle">
                Agent reasoning
              </p>
              <p className="font-mono text-[0.78rem] leading-relaxed text-foreground/90">
                {step.reasoning}
              </p>
            </div>
          ) : null}

          <DraftedMessage step={step} />

          {step.action_details ? (
            <p className="text-xs leading-relaxed text-muted">{step.action_details}</p>
          ) : null}

          <div className="flex flex-wrap items-center gap-4 text-xs text-muted">
            {step.cost > 0 ? (
              <span className="tnum">Cost {currency(step.cost, currencyCode)}</span>
            ) : null}
            {step.recovered_amount > 0 ? (
              <span className="tnum text-success">
                Recovered {currency(step.recovered_amount, currencyCode)}
              </span>
            ) : null}

            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              className="ml-auto inline-flex items-center gap-1 text-xs text-muted hover:text-foreground"
            >
              <ChevronDown
                size={13}
                className={cn('transition-transform', open && 'rotate-180')}
              />
              {open ? 'Hide' : 'Show'} decision context
            </button>
          </div>

          {/* The audit payload: exactly what the decision-maker was looking at.
              Kept collapsed because it is for the moment someone disputes a
              decision, not for routine scanning. */}
          <AnimatePresence initial={false}>
            {open ? (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <p className="mb-1.5 text-[0.62rem] font-semibold uppercase tracking-[0.08em] text-subtle">
                  Context snapshot · frozen at decision time
                </p>
                <pre className="max-h-80 overflow-auto rounded-lg border border-border bg-background p-3 font-mono text-[0.7rem] leading-relaxed text-muted">
                  {JSON.stringify(step.context_snapshot, null, 2)}
                </pre>
                <p className="mt-1.5 font-mono text-[0.65rem] text-subtle">
                  intent_id {step.intent_id}
                </p>
              </motion.div>
            ) : null}
          </AnimatePresence>
        </div>
      </div>
    </div>
  )
}

export function TraceTimeline({
  steps,
  currencyCode,
}: {
  steps: CaseStep[]
  currencyCode: string
}) {
  return (
    <div className="space-y-3">
      <AnimatePresence initial={false}>
        {steps.map((step) => (
          <motion.div
            key={step.id}
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3 }}
          >
            <StepCard step={step} currencyCode={currencyCode} />
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  )
}
