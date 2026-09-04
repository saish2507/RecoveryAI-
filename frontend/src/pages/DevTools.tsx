/**
 * Dev Tools — inject events, force follow-ups, inspect governance state.
 *
 * Deliberately a utility drawer rather than the centrepiece the previous build
 * made it. A manual injector as the main screen implies a system that needs a
 * human to feed it; the product surface is the queue and the trace.
 */

import { FlaskConical, PlayCircle, Send, Zap } from 'lucide-react'
import { Link } from 'react-router-dom'
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Skeleton,
  Tooltip,
} from '@/components/ui'
import { useInjectEvent, useRunFollowups, useScenarios, useSystemStatus } from '@/lib/api'
import { humanise, relativeTime } from '@/lib/format'
import type { Vertical } from '@/lib/types'

const VERTICALS: { value: Vertical; label: string }[] = [
  { value: 'cart', label: 'Checkout' },
  { value: 'b2b', label: 'Receivables' },
  { value: 'autopay', label: 'Autopay' },
]

function StatusRow({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <Tooltip content={hint}>
        <span className="cursor-default text-xs text-muted">{label}</span>
      </Tooltip>
      <span className="tnum text-sm">{value}</span>
    </div>
  )
}

export function DevTools() {
  const scenarios = useScenarios()
  const status = useSystemStatus()
  const inject = useInjectEvent()
  const followups = useRunFollowups()

  const llm = status.data?.agent.llm

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-bold">Dev Tools</h1>
        <p className="text-xs text-muted">
          Drive the agent by hand. Injected events run through the same code path as a real webhook —
          no shortcuts, no special casing.
        </p>
      </div>

      <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Named scenarios</CardTitle>
              <p className="mt-1 text-xs leading-relaxed text-muted">
                Deterministic fixtures against a stable demo customer per vertical, so injecting the
                same scenario twice accumulates guardrail state — that is how you watch a guardrail
                actually fire rather than describing one.
              </p>
            </CardHeader>
            <CardContent className="space-y-2">
              {scenarios.isLoading ? (
                Array.from({ length: 4 }).map((_, index) => <Skeleton key={index} className="h-14" />)
              ) : (
                Object.entries(scenarios.data ?? {}).map(([name, description]) => (
                  <div
                    key={name}
                    className="flex items-start gap-3 rounded-lg border border-border bg-background px-3 py-2.5"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="font-mono text-xs font-semibold text-foreground">{name}</p>
                      <p className="mt-0.5 text-xs leading-relaxed text-muted">{description}</p>
                    </div>
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={inject.isPending}
                      onClick={() => inject.mutate({ scenario: name })}
                    >
                      <Send size={13} />
                      Inject
                    </Button>
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Random event</CardTitle>
              <p className="mt-1 text-xs text-muted">
                Drawn from the simulator's realistic mix, including gateway codes the rules do not
                recognise.
              </p>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-2">
              {VERTICALS.map(({ value, label }) => (
                <Button
                  key={value}
                  size="sm"
                  disabled={inject.isPending}
                  onClick={() => inject.mutate({ vertical: value })}
                >
                  <FlaskConical size={13} />
                  {label}
                </Button>
              ))}
              <Button
                size="sm"
                variant="primary"
                disabled={inject.isPending}
                onClick={() => inject.mutate({})}
              >
                <Zap size={13} />
                Any vertical
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Follow-up sweep</CardTitle>
              <p className="mt-1 text-xs leading-relaxed text-muted">
                Re-evaluates every case whose follow-up is due, instead of waiting for the next
                scheduled tick.
              </p>
            </CardHeader>
            <CardContent className="space-y-3">
              <Button
                size="sm"
                variant="primary"
                disabled={followups.isPending}
                onClick={() => followups.mutate()}
              >
                <PlayCircle size={14} />
                {followups.isPending ? 'Running…' : 'Run now'}
              </Button>

              {followups.data ? (
                followups.data.advanced === 0 ? (
                  <p className="text-xs text-muted">
                    No cases were due. Follow-ups are scheduled{' '}
                    {status.data?.agent.followup_delay_seconds ?? '—'}s after each decision.
                  </p>
                ) : (
                  <ul className="space-y-1.5">
                    {followups.data.cases.map((entry: any) => (
                      <li
                        key={String(entry.case_id)}
                        className="flex items-center gap-2 rounded-lg border border-border bg-background px-3 py-2 text-xs"
                      >
                        <Link
                          to={`/cases/${entry.case_id}`}
                          className="font-mono text-accent-hover hover:underline"
                        >
                          {String(entry.case_id)}
                        </Link>
                        <span className="text-subtle">step {String(entry.step)}</span>
                        <span className="ml-auto">{humanise(String(entry.final_action))}</span>
                        {entry.guardrail_verdict === 'blocked' ? (
                          <Badge tone="danger">Redirected</Badge>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                )
              ) : null}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-4">
          {inject.data ? (
            <Card className="border-accent/40">
              <CardHeader>
                <CardTitle>Last injection</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                <Link
                  to={`/cases/${inject.data.id}`}
                  className="block font-mono text-xs text-accent-hover hover:underline"
                >
                  {inject.data.id}
                </Link>
                <p className="text-xs text-muted">
                  {humanise(inject.data.diagnosis)} · {inject.data.status}
                </p>
                <Button asChild size="sm" variant="primary" className="w-full">
                  <Link to={`/cases/${inject.data.id}`}>View trace</Link>
                </Button>
              </CardContent>
            </Card>
          ) : null}

          {inject.isError ? (
            <Card className="border-danger/40">
              <CardContent className="text-xs text-danger">
                {(inject.error as Error).message}
              </CardContent>
            </Card>
          ) : null}

          <Card>
            <CardHeader>
              <CardTitle>LLM governance</CardTitle>
            </CardHeader>
            <CardContent className="divide-y divide-border py-0">
              {!llm ? (
                <Skeleton className="my-3 h-20" />
              ) : (
                <>
                  <StatusRow label="Provider" value={llm.provider} />
                  <StatusRow
                    label="Configured"
                    value={llm.configured ? 'yes' : 'no — rules only'}
                    hint="With no key the agent runs fully deterministically and makes zero network calls."
                  />
                  <StatusRow
                    label="This minute"
                    value={`${llm.minute_tokens_remaining} / ${llm.minute_capacity}`}
                    hint="Token-bucket rate limiter. At zero, decisions fall back to the policy tables."
                  />
                  <StatusRow
                    label="Today"
                    value={`${llm.day_tokens_remaining} / ${llm.day_capacity}`}
                    hint="Daily budget, persisted in SQLite so it survives a restart."
                  />
                  <StatusRow
                    label="Cached decisions"
                    value={String(llm.cache_entries)}
                    hint="Identical cases reuse a prior decision at no budget cost."
                  />
                </>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Runtime</CardTitle>
            </CardHeader>
            <CardContent className="divide-y divide-border py-0">
              {!status.data ? (
                <Skeleton className="my-3 h-20" />
              ) : (
                <>
                  <StatusRow
                    label="Mode"
                    value={status.data.agent.agent_mode}
                    hint="shadow = decide and log everything, dispatch nothing."
                  />
                  <StatusRow label="Executor" value={status.data.agent.executor} />
                  <StatusRow label="Max steps" value={String(status.data.agent.max_case_steps)} />
                  <StatusRow
                    label="Scheduler"
                    value={status.data.scheduler.running ? 'running' : 'stopped'}
                  />
                  <StatusRow
                    label="Re-evaluations"
                    value={String(status.data.scheduler.decisions_made)}
                  />
                  <StatusRow
                    label="Last sweep"
                    value={
                      status.data.scheduler.last_run_at
                        ? relativeTime(status.data.scheduler.last_run_at)
                        : '—'
                    }
                  />
                  <StatusRow
                    label="Consoles connected"
                    value={String(status.data.websocket.connections)}
                  />
                </>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}
