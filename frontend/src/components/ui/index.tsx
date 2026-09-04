/**
 * The component foundation — shadcn/ui's approach (Radix primitives + Tailwind +
 * CVA variants), kept in one file because the set is small enough that spreading
 * it across a dozen files would cost more in navigation than it saves.
 *
 * These are owned components, not a dependency: every variant reads from the
 * theme tokens in `index.css`, so a palette change propagates everywhere.
 */

import { Slot } from '@radix-ui/react-slot'
import * as TabsPrimitive from '@radix-ui/react-tabs'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import { cva, type VariantProps } from 'class-variance-authority'
import { clsx, type ClassValue } from 'clsx'
import type { ComponentProps, ReactNode } from 'react'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// ── Button ───────────────────────────────────────────────────────

const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 rounded-lg text-sm font-semibold ' +
    'transition-colors disabled:pointer-events-none disabled:opacity-50 ' +
    'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent',
  {
    variants: {
      variant: {
        primary: 'bg-accent text-white hover:bg-accent-hover',
        secondary: 'border border-border bg-transparent text-foreground hover:border-accent hover:text-accent-hover',
        ghost: 'text-muted hover:bg-surface-hover hover:text-foreground',
        danger: 'bg-danger-soft text-danger hover:bg-danger hover:text-white',
      },
      size: {
        sm: 'h-8 px-3 text-xs',
        md: 'h-9 px-4',
        lg: 'h-10 px-5',
        icon: 'h-8 w-8',
      },
    },
    defaultVariants: { variant: 'secondary', size: 'md' },
  },
)

export interface ButtonProps
  extends ComponentProps<'button'>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

export function Button({ className, variant, size, asChild, ...props }: ButtonProps) {
  const Comp = asChild ? Slot : 'button'
  return <Comp className={cn(buttonVariants({ variant, size }), className)} {...props} />
}

// ── Card ─────────────────────────────────────────────────────────

export function Card({ className, ...props }: ComponentProps<'div'>) {
  return (
    <div
      className={cn('rounded-card border border-border bg-surface overflow-hidden', className)}
      {...props}
    />
  )
}

export function CardHeader({ className, ...props }: ComponentProps<'div'>) {
  return <div className={cn('px-5 py-4 border-b border-border', className)} {...props} />
}

export function CardTitle({ className, ...props }: ComponentProps<'h3'>) {
  return (
    <h3
      className={cn(
        'text-[0.7rem] font-medium uppercase tracking-[0.06em] text-muted',
        className,
      )}
      {...props}
    />
  )
}

export function CardContent({ className, ...props }: ComponentProps<'div'>) {
  return <div className={cn('px-5 py-4', className)} {...props} />
}

// ── Badge ────────────────────────────────────────────────────────

const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[0.68rem] font-bold ' +
    'uppercase tracking-[0.04em] border whitespace-nowrap',
  {
    variants: {
      tone: {
        neutral: 'border-border text-muted bg-background',
        accent: 'border-accent text-accent-hover bg-accent-soft',
        success: 'border-success/50 text-success bg-success-soft',
        warning: 'border-warning/50 text-warning bg-warning-soft',
        danger: 'border-danger/50 text-danger bg-danger-soft',
        info: 'border-info/50 text-info bg-info-soft',
      },
    },
    defaultVariants: { tone: 'neutral' },
  },
)

export interface BadgeProps extends ComponentProps<'span'>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />
}

// ── Tooltip ──────────────────────────────────────────────────────

export function TooltipProvider({ children }: { children: ReactNode }) {
  return <TooltipPrimitive.Provider delayDuration={200}>{children}</TooltipPrimitive.Provider>
}

export function Tooltip({ content, children }: { content: ReactNode; children: ReactNode }) {
  if (!content) return <>{children}</>
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          sideOffset={6}
          className="z-50 max-w-xs rounded-lg border border-border-strong bg-surface px-3 py-2 text-xs leading-relaxed text-foreground shadow-xl"
        >
          {content}
          <TooltipPrimitive.Arrow className="fill-[var(--color-border-strong)]" />
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  )
}

// ── Tabs ─────────────────────────────────────────────────────────

export const Tabs = TabsPrimitive.Root

export function TabsList({ className, ...props }: ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      className={cn('inline-flex gap-1 rounded-lg border border-border bg-surface p-1', className)}
      {...props}
    />
  )
}

export function TabsTrigger({ className, ...props }: ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        'rounded-md px-3 py-1.5 text-xs font-semibold text-muted transition-colors',
        'hover:text-foreground data-[state=active]:bg-accent-soft data-[state=active]:text-accent-hover',
        className,
      )}
      {...props}
    />
  )
}

export const TabsContent = TabsPrimitive.Content

// ── Form controls ────────────────────────────────────────────────

export function Select({ className, ...props }: ComponentProps<'select'>) {
  return (
    <select
      className={cn(
        'h-9 rounded-lg border border-border bg-background px-3 text-sm text-foreground',
        'focus:border-accent focus:outline-none',
        className,
      )}
      {...props}
    />
  )
}

// ── States ───────────────────────────────────────────────────────

export function Skeleton({ className, ...props }: ComponentProps<'div'>) {
  return <div className={cn('animate-pulse rounded-md bg-surface-hover', className)} {...props} />
}

/**
 * Empty states say what will fill the space and how to make that happen. "No
 * data" tells a user nothing about whether the system is broken or simply idle.
 */
export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
      {icon ? <div className="text-subtle">{icon}</div> : null}
      <div>
        <p className="text-sm font-semibold text-foreground">{title}</p>
        {description ? (
          <p className="mt-1 max-w-sm text-xs leading-relaxed text-muted">{description}</p>
        ) : null}
      </div>
      {action}
    </div>
  )
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-12 text-center">
      <p className="text-sm font-semibold text-danger">Could not load this</p>
      <p className="max-w-md text-xs leading-relaxed text-muted">{message}</p>
      {onRetry ? (
        <Button size="sm" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  )
}
