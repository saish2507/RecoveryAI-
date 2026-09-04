/** Portfolio view: what is at risk, what came back, and how the agent is deciding. */

import {
  Activity,
  AlertTriangle,
  Bot,
  Coins,
  Inbox,
  ShieldCheck,
  TrendingUp,
  Wallet,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useAnomalies, useCases, useMetrics, useSystemStatus } from '@/lib/api'
import {
  CASE_STATUS_LABELS,
  DECISION_SOURCE_LABELS,
  VERTICAL_LABELS,
  caseStatusTone,
  compactCurrency,
  currency,
  humanise,
  percent,
  relativeTime,
} from '@/lib/format'
import type { CaseStatus, DecisionSource, Vertical } from '@/lib/types'
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

function Stat({
  icon,
  label,
  value,
  sub,
  hint,
  badge,
}: {
  icon: ReactNode
  label: string
  value: string
  sub?: string
  hint?: string
  badge?: ReactNode
}) {
  return (
    <Card>
      <CardContent className="space-y-1">
        <div className="flex items-center gap-2 text-muted">
          {icon}
          <Tooltip content={hint}>
            <span className="cursor-default text-[0.68rem] font-medium uppercase tracking-[0.06em]">
              {label}
            </span>
          </Tooltip>
          {badge ? <span className="ml-auto">{badge}</span> : null}
        </div>
        <p className="tnum text-2xl font-bold text-foreground">{value}</p>
        {sub ? <p className="text-xs text-muted">{sub}</p> : null}
      </CardContent>
    </Card>
  )
}

/** A labelled proportion bar. Used for the breakdowns that are shares of a whole. */
function ShareBar({ data, total }: { data: [string, number, string][]; total: number }) {
  if (total === 0) {
    return <p className="text-xs text-subtle">Nothing recorded yet.</p>
  }
  return (
    <div className="space-y-3">
      <div className="flex h-2 overflow-hidden rounded-full bg-background">
        {data.map(([key, count, color]) => (
          <div
            key={key}
            style={{ width: `${(count / total) * 100}%`, background: color }}
            className="h-full"
          />
        ))}
      </div>
      <div className="space-y-1.5">
        {data.map(([key, count, color]) => (
          <div key={key} className="flex items-center gap-2 text-xs">
            <span className="h-2 w-2 rounded-full" style={{ background: color }} />
            <span className="text-muted">{key}</span>
            <span className="tnum ml-auto font-medium text-foreground">{count}</span>
            <span className="tnum w-10 text-right text-subtle">{percent(count / total)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

const SOURCE_COLORS: Record<string, string> = {
  llm: 'var(--color-accent)',
  llm_cached: 'var(--color-accent-hover)',
}

const VERTICAL_COLORS: Record<string, string> = {
  cart: 'var(--color-accent)',
  b2b: 'var(--color-info)',
  autopay: 'var(--color-warning)',
}

/**
 * Signals that only exist across many cases at once.
 *
 * Every other view here answers a question about one case. Forty correctly
 * diagnosed `mandate_broken` cases are forty good decisions and one missed
 * incident, and no per-case view can show the second thing — an issuer changing
 * something upstream looks completely normal one row at a time.
 *
 * Hidden entirely when nothing is firing. A panel that is usually empty teaches
 * people to stop reading it, and this is the one that most needs reading when it
 * does have something to say.
 */
function AnomalyPanel() {
  const anomalies = useAnomalies()
  const found = anomalies.data?.anomalies ?? []
  if (!found.length) return null

  const tone = (severity: string) =>
    severity === 'high' ? 'danger' : severity === 'medium' ? 'warning' : 'info'

  return (
    <Card className="border-warning/40">
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Activity size={14} className="text-warning" />
          Unusual activity
        </CardTitle>
        <span className="text-xs text-subtle">
          last {anomalies.data?.window_minutes ?? 60}m vs {anomalies.data?.baseline_hours ?? 24}h
          baseline
        </span>
      </CardHeader>
      <CardContent className="space-y-2">
        {found.slice(0, 4).map((anomaly) => (
          <div
            key={`${anomaly.kind}:${anomaly.key}`}
            className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border bg-background px-3 py-2.5"
          >
            <Badge tone={tone(anomaly.severity)}>{anomaly.severity}</Badge>
            <span className="font-medium">{humanise(anomaly.label)}</span>
            <span className="tnum text-xs text-muted">
              {anomaly.recent_count} cases · {compactCurrency(anomaly.amount_at_risk)} at risk
            </span>
            <span className="w-full text-xs text-subtle sm:ml-auto sm:w-auto">{anomaly.detail}</span>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

export function Dashboard() {
  const metrics = useMetrics()
  const status = useSystemStatus()
  const recent = useCases({ limit: 8 })

  if (metrics.isError) {
    return (
      <ErrorState
        message={(metrics.error as Error).message}
        onRetry={() => void metrics.refetch()}
      />
    )
  }

  if (metrics.isLoading || !metrics.data) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 8 }).map((_, index) => (
          <Skeleton key={index} className="h-24" />
        ))}
      </div>
    )
  }

  const { cases, revenue, decisions } = metrics.data

  // Until a host reports real outcomes, every recovery figure is the simulated
  // executor's estimate. Saying so on the tile itself matters more than saying
  // it in a tooltip nobody hovers — an unqualified number reads as measured.
  const simulatedBadge = (
    <Tooltip content="No host has reported a real execution outcome yet, so these figures come from the built-in simulated executor. Report outcomes to POST /api/v1/actions/{intent_id}/report to replace them with measured results.">
      <Badge tone="warning" className="cursor-default">
        Est.
      </Badge>
    </Tooltip>
  )
  const sourceData: [string, number, string][] = Object.entries(decisions.by_source).map(
    // The label map, not `humanise` — the raw enum renders as 'Llm', and a
    // chart disagreeing with the trace timeline about what to call the same
    // thing reads as two different systems.
    ([key, count]) => [
      DECISION_SOURCE_LABELS[key as DecisionSource]?.label ?? humanise(key),
      count,
      SOURCE_COLORS[key] ?? 'var(--color-muted)',
    ],
  )
  const verticalData: [string, number, string][] = Object.entries(cases.by_vertical).map(
    ([key, count]) => [
      VERTICAL_LABELS[key as Vertical] ?? key,
      count,
      VERTICAL_COLORS[key] ?? 'var(--color-muted)',
    ],
  )

  return (
    <div className="space-y-6">
      <AnomalyPanel />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          icon={<Wallet size={14} />}
          label="Revenue at risk"
          value={compactCurrency(revenue.at_risk)}
          sub={`${cases.total} cases · ${cases.open} still open`}
          hint="Total value of every case the agent has taken in."
        />
        <Stat
          icon={<TrendingUp size={14} />}
          label="Recovered"
          value={compactCurrency(revenue.recovered)}
          sub={`${percent(revenue.recovery_rate, 1)} of at-risk value`}
          hint="Estimated by the simulated executor unless a host is reporting real outcomes."
          badge={simulatedBadge}
        />
        <Stat
          icon={<Coins size={14} />}
          label="Net recovered"
          value={compactCurrency(revenue.net_recovered)}
          sub={`after ${currency(revenue.cost_of_recovery)} of recovery cost`}
          hint="Recovered minus what was spent recovering it. Discounts are the expensive lever."
          badge={simulatedBadge}
        />
        <Stat
          icon={<ShieldCheck size={14} />}
          label="Guardrail blocks"
          value={String(decisions.guardrail_blocks)}
          sub={`of ${decisions.steps} decisions`}
          hint="Times a proposed action was refused in code and deterministically redirected."
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>Who decided</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <ShareBar data={sourceData} total={decisions.steps} />
            <p className="border-t border-border pt-3 text-xs leading-relaxed text-muted">
              <Bot size={12} className="mr-1 inline" />
              Every case is sent to the model. {percent(decisions.llm_call_share, 1)} of steps spent
              a call attempt; the <span className="text-foreground">Llm</span> share above is the
              narrower number — steps the model actually decided. The gap is calls that were
              attempted and refused on quota, which fall back to the policy tables and are recorded
              as such rather than hidden.
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Cases by vertical</CardTitle>
          </CardHeader>
          <CardContent>
            <ShareBar data={verticalData} total={cases.total} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Lifecycle</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {Object.entries(cases.by_status).length === 0 ? (
              <p className="text-xs text-subtle">No cases yet.</p>
            ) : (
              Object.entries(cases.by_status).map(([key, count]) => (
                <div key={key} className="flex items-center gap-2">
                  <Badge tone={caseStatusTone(key as CaseStatus)}>
                    {CASE_STATUS_LABELS[key as CaseStatus] ?? key}
                  </Badge>
                  <span className="tnum ml-auto text-sm font-medium">{count}</span>
                </div>
              ))
            )}
            {status.data?.scheduler ? (
              <p className="border-t border-border pt-3 text-xs text-muted">
                Follow-up sweep: {status.data.scheduler.running ? 'running' : 'stopped'} ·{' '}
                {status.data.scheduler.decisions_made} re-evaluations
                {status.data.scheduler.errors > 0 ? (
                  <span className="text-warning">
                    {' '}
                    · {status.data.scheduler.errors} errors
                  </span>
                ) : null}
              </p>
            ) : null}
          </CardContent>
        </Card>
      </div>

      {status.data?.security.warnings.length ? (
        <Card className="border-warning/40">
          <CardContent className="flex items-start gap-3">
            <AlertTriangle size={16} className="mt-0.5 shrink-0 text-warning" />
            <div className="space-y-1">
              <p className="text-sm font-semibold text-warning">Security posture</p>
              {status.data.security.warnings.map((warning) => (
                <p key={warning} className="text-xs leading-relaxed text-muted">
                  {warning}
                </p>
              ))}
            </div>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>Latest cases</CardTitle>
          <Button asChild size="sm" variant="ghost">
            <Link to="/cases">View queue</Link>
          </Button>
        </CardHeader>
        <CardContent className="p-0">
          {recent.data?.items.length ? (
            <ul className="divide-y divide-border">
              {recent.data.items.map((item) => (
                <li key={item.id}>
                  <Link
                    to={`/cases/${item.id}`}
                    className="flex items-center gap-3 px-5 py-3 transition-colors hover:bg-surface-hover"
                  >
                    <Badge tone="accent">{VERTICAL_LABELS[item.vertical] ?? item.vertical}</Badge>
                    <span className="tnum text-sm font-medium">
                      {currency(item.amount, item.currency)}
                    </span>
                    <span className="truncate text-xs text-muted">{humanise(item.diagnosis)}</span>
                    <Badge tone={caseStatusTone(item.status)} className="ml-auto">
                      {CASE_STATUS_LABELS[item.status] ?? item.status}
                    </Badge>
                    <span className="w-16 shrink-0 text-right text-xs text-subtle">
                      {relativeTime(item.created_at)}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              icon={<Inbox size={26} />}
              title="No cases yet"
              description="Cases appear here as events arrive. Use Dev Tools to inject one, or POST to /api/v1/events."
              action={
                <Button asChild size="sm" variant="primary">
                  <Link to="/dev">Open Dev Tools</Link>
                </Button>
              }
            />
          )}
        </CardContent>
      </Card>
    </div>
  )
}
