/** Chrome: navigation, live-connection indicator, and the mode banner. */

import { Activity, AlertTriangle, LayoutDashboard, ListChecks, TerminalSquare, UserCheck } from 'lucide-react'
import { NavLink } from 'react-router-dom'
import type { ReactNode } from 'react'
import { useSetLlmEnabled, useSystemStatus } from '@/lib/api'
import { useWs } from '@/lib/ws'
import { Badge, Tooltip, cn } from '@/components/ui'

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/cases', label: 'Case Queue', icon: ListChecks, end: false },
  { to: '/review', label: 'Review', icon: UserCheck, end: false },
  { to: '/dev', label: 'Dev Tools', icon: TerminalSquare, end: false },
]

function ConnectionPill() {
  const { state } = useWs()
  const tone = state === 'open' ? 'success' : state === 'connecting' ? 'warning' : 'danger'
  const label = state === 'open' ? 'Live' : state === 'connecting' ? 'Connecting' : 'Offline'
  const hint =
    state === 'open'
      ? 'Connected. Updates arrive without a refresh.'
      : state === 'connecting'
        ? 'Reconnecting with exponential backoff.'
        : 'Disconnected. The console will keep retrying; data may be stale.'

  return (
    <Tooltip content={hint}>
      <Badge tone={tone} className="cursor-default">
        <span
          className={cn(
            'inline-block h-1.5 w-1.5 rounded-full',
            state === 'open' && 'bg-success animate-pulse',
            state === 'connecting' && 'bg-warning animate-pulse',
            state === 'closed' && 'bg-danger',
          )}
        />
        {label}
      </Badge>
    </Tooltip>
  )
}

/**
 * The model switch, doubling as the budget readout.
 *
 * One control rather than a badge plus a settings page: "how much model budget
 * is left" and "should the model be used at all" are the same decision, and an
 * operator watching the budget drain is exactly the person who wants to stop it.
 *
 * Disabled without a key, because there is nothing to switch — the label says
 * so instead of offering a control that would silently do nothing.
 */
export function LlmSwitch() {
  const { data } = useSystemStatus()
  const setEnabled = useSetLlmEnabled()
  const llm = data?.agent.llm
  if (!llm) return null

  const on = llm.configured && llm.enabled
  const label = !llm.configured
    ? 'No LLM key'
    : on
      ? `LLM ${llm.day_tokens_remaining}/${llm.day_capacity}`
      : 'Rules only'

  const hint = !llm.configured
    ? 'No LLM key configured, so there is nothing to switch. Every decision comes from the ' +
      'rule engine and policy tables. Set GEMINI_API_KEY and restart to enable model reasoning.'
    : on
      ? `${llm.day_tokens_remaining} of ${llm.day_capacity} model calls left today, ` +
        `${llm.minute_tokens_remaining}/${llm.minute_capacity} this minute. Ambiguous cases go to ` +
        `${llm.provider}; confident ones are still decided by rules at no cost. Click to switch off.`
      : 'Model consultation is off. Events keep arriving and cases keep progressing — every ' +
        'decision comes from the rule engine, at no cost. Click to switch back on.'

  return (
    <Tooltip content={hint}>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label="Model consultation"
        disabled={!llm.configured || setEnabled.isPending}
        onClick={() => setEnabled.mutate(!llm.enabled)}
        className={cn(
          'inline-flex items-center gap-2 whitespace-nowrap rounded-md border px-2 py-0.5',
          'text-[0.68rem] font-bold uppercase tracking-[0.04em] transition-colors',
          'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent',
          on ? 'border-accent bg-accent-soft text-accent-hover' : 'border-border bg-background text-muted',
          llm.configured ? 'cursor-pointer hover:border-accent-hover' : 'cursor-not-allowed opacity-60',
          setEnabled.isPending && 'opacity-60',
        )}
      >
        <span
          className={cn(
            'relative inline-block h-3 w-5 shrink-0 rounded-full transition-colors',
            on ? 'bg-accent' : 'bg-border-strong',
          )}
        >
          <span
            className={cn(
              'absolute top-[3px] h-1.5 w-1.5 rounded-full bg-white transition-all',
              on ? 'left-[11px]' : 'left-[3px]',
            )}
          />
        </span>
        {label}
      </button>
    </Tooltip>
  )
}

/**
 * Shadow mode is a persistent, unmissable banner rather than a subtle badge.
 * Someone reading a decision trace needs to know whether it describes something
 * that happened or something that merely would have.
 */
function ModeBanner() {
  const { data } = useSystemStatus()
  if (data?.agent.agent_mode !== 'shadow') return null

  return (
    <div className="flex items-center justify-center gap-2 border-b border-warning/30 bg-warning-soft px-4 py-2 text-xs text-warning">
      <AlertTriangle size={14} />
      <span>
        <strong className="font-semibold">Shadow mode.</strong> Every decision is reasoned, guardrail-checked
        and logged in full — nothing is dispatched anywhere.
      </span>
    </div>
  )
}

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-background">
      <ModeBanner />

      <header className="sticky top-0 z-40 border-b border-border bg-surface/95 backdrop-blur">
        <div className="mx-auto flex max-w-[1400px] items-center gap-6 px-6 py-3">
          <NavLink to="/" className="flex items-center gap-2 text-[0.95rem] font-bold tracking-tight">
            <Activity size={18} className="text-accent-hover" />
            RecoveryAI
          </NavLink>

          <nav className="flex items-center gap-1">
            {NAV.map(({ to, label, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  cn(
                    'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors',
                    isActive
                      ? 'bg-accent-soft text-accent-hover'
                      : 'text-muted hover:bg-surface-hover hover:text-foreground',
                  )
                }
              >
                <Icon size={15} />
                {label}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2">
            <LlmSwitch />
            <ConnectionPill />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1400px] px-6 py-6">{children}</main>
    </div>
  )
}
