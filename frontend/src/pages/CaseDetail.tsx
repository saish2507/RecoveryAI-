/**
 * Case detail — the case facts on the left, the full decision trace on the right.
 *
 * The trace is the point of the page. Everything else is context for reading it.
 */

import { ArrowLeft, Clock, PlayCircle } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { TraceTimeline } from '@/components/TraceTimeline'
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  EmptyState,
  ErrorState,
  Skeleton,
  Tooltip,
} from '@/components/ui'
import { useAdvanceCase, useCase } from '@/lib/api'
import {
  CASE_STATUS_LABELS,
  VERTICAL_LABELS,
  absoluteTime,
  caseStatusTone,
  confidenceTone,
  currency,
  humanise,
  percent,
  relativeTime,
} from '@/lib/format'

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 py-2">
      <span className="shrink-0 text-xs text-muted">{label}</span>
      <span className="text-right text-sm">{children}</span>
    </div>
  )
}

export function CaseDetail() {
  const { caseId } = useParams<{ caseId: string }>()
  const query = useCase(caseId)
  const advance = useAdvanceCase()

  if (query.isError) {
    return (
      <ErrorState message={(query.error as Error).message} onRetry={() => void query.refetch()} />
    )
  }

  if (query.isLoading || !query.data) {
    return (
      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        <Skeleton className="h-96" />
        <Skeleton className="h-96" />
      </div>
    )
  }

  const item = query.data
  const closed = ['resolved', 'escalated', 'abandoned'].includes(item.status)
  const signals = Object.entries(item.vertical_metadata ?? {})

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button asChild size="icon" variant="ghost">
          <Link to="/cases" aria-label="Back to case queue">
            <ArrowLeft size={16} />
          </Link>
        </Button>
        <div>
          <h1 className="font-mono text-sm font-semibold">{item.id}</h1>
          <p className="text-xs text-muted">
            Opened {relativeTime(item.created_at)} · {item.step_count} step
            {item.step_count === 1 ? '' : 's'}
          </p>
        </div>

        <div className="ml-auto flex items-center gap-2">
          {item.next_followup_at ? (
            <Tooltip content={`Next re-evaluation at ${absoluteTime(item.next_followup_at)}`}>
              <Badge tone="info" className="cursor-default">
                <Clock size={11} />
                Follow-up scheduled
              </Badge>
            </Tooltip>
          ) : null}
          <Badge tone={caseStatusTone(item.status)}>
            {CASE_STATUS_LABELS[item.status] ?? item.status}
          </Badge>
          <Tooltip
            content={
              closed
                ? 'This case is closed. No further steps will be taken.'
                : 'Run the next decision step now instead of waiting for the scheduled follow-up.'
            }
          >
            <span>
              <Button
                size="sm"
                variant="primary"
                disabled={closed || advance.isPending}
                onClick={() => caseId && advance.mutate(caseId)}
              >
                <PlayCircle size={14} />
                {advance.isPending ? 'Deciding…' : 'Advance step'}
              </Button>
            </span>
          </Tooltip>
        </div>
      </div>

      {advance.isError ? (
        <Card className="border-danger/40">
          <CardContent className="text-xs text-danger">
            {(advance.error as Error).message}
          </CardContent>
        </Card>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[340px_1fr]">
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Case</CardTitle>
            </CardHeader>
            <CardContent className="divide-y divide-border py-0">
              <Field label="Vertical">
                <Badge tone="accent">{VERTICAL_LABELS[item.vertical] ?? item.vertical}</Badge>
              </Field>
              <Field label="At risk">
                <span className="tnum font-semibold">
                  {currency(item.amount, item.currency)}
                </span>
              </Field>
              <Field label="Customer">
                <span className="font-mono text-xs">{item.customer_id}</span>
              </Field>
              <Field label="LTV tier">{humanise(item.ltv_tier)}</Field>
              <Field label="Diagnosis">{humanise(item.diagnosis)}</Field>
              <Field label="Failure reason">
                {/* Rendered verbatim in mono: this is the gateway's string, not
                    ours, and may be a code nobody has documented. */}
                <span className="font-mono text-xs break-all">
                  {item.raw_failure_reason || '—'}
                </span>
              </Field>
              {item.latest_confidence !== null ? (
                <Field label="Latest confidence">
                  <Badge tone={confidenceTone(item.latest_confidence)}>
                    {percent(item.latest_confidence)}
                  </Badge>
                </Field>
              ) : null}
              <Field label="Priority score">
                <span className="tnum">{Math.round(item.priority_score).toLocaleString('en-IN')}</span>
              </Field>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Economics</CardTitle>
            </CardHeader>
            <CardContent className="divide-y divide-border py-0">
              <Field label="Recovered">
                <span className="tnum text-success">
                  {currency(item.amount_recovered, item.currency)}
                </span>
              </Field>
              <Field label="Cost of recovery">
                <span className="tnum">{currency(item.cost_of_recovery, item.currency)}</span>
              </Field>
              <Field label="Net">
                <span className="tnum font-semibold">
                  {currency(item.amount_recovered - item.cost_of_recovery, item.currency)}
                </span>
              </Field>
            </CardContent>
          </Card>

          {signals.length ? (
            <Card>
              <CardHeader>
                <CardTitle>Signals</CardTitle>
              </CardHeader>
              <CardContent className="divide-y divide-border py-0">
                {signals.map(([key, value]) => (
                  <Field key={key} label={humanise(key)}>
                    <span className="font-mono text-xs">
                      {value === null || value === undefined ? '—' : String(value)}
                    </span>
                  </Field>
                ))}
              </CardContent>
            </Card>
          ) : null}
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Decision trace</CardTitle>
            <p className="mt-1 text-xs leading-relaxed text-muted">
              Every step in order: who decided, what they proposed, what the guardrail said, and what
              actually ran.
            </p>
          </CardHeader>
          <CardContent>
            {item.steps.length ? (
              <TraceTimeline steps={item.steps} currencyCode={item.currency} />
            ) : (
              <EmptyState
                title="No decisions yet"
                description="This case was created but has not been worked. Advance a step to see the agent reason about it."
              />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
