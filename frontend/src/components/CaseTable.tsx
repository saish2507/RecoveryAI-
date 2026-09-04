/**
 * The shared, sortable case table used by both queues.
 *
 * TanStack Table handles sorting; server-side ordering is by `priority_score`,
 * so the default view is already the right one and client sorting is for a user
 * who wants to look at the same data another way — not a substitute for the
 * server getting the order right.
 */

import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from '@tanstack/react-table'
import { AnimatePresence, motion } from 'framer-motion'
import { ArrowDown, ArrowUp, ChevronsUpDown } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge, Tooltip, cn } from '@/components/ui'
import {
  CASE_STATUS_LABELS,
  VERTICAL_LABELS,
  caseStatusTone,
  compactCurrency,
  confidenceTone,
  currency,
  priorityBand,
  humanise,
  percent,
  relativeTime,
} from '@/lib/format'
import type { Case } from '@/lib/types'

interface CaseTableProps {
  cases: Case[]
  /** Review queue explains the score differently from the work queue. */
  scoreMeaning: 'intake' | 'review'
}

export function CaseTable({ cases, scoreMeaning }: CaseTableProps) {
  const navigate = useNavigate()
  const [sorting, setSorting] = useState<SortingState>([])

  const columns = useMemo<ColumnDef<Case>[]>(
    () => [
      {
        accessorKey: 'priority_score',
        header: scoreMeaning === 'review' ? 'Review priority' : 'Priority',
        cell: ({ row }) => (
          <Tooltip
            content={
              scoreMeaning === 'review'
                ? `(1 − confidence) × amount. A large case the agent was unsure about outranks a small
                   one that only hit a guardrail limit.`
                : 'amount × urgency. The largest at-risk revenue is worked first, not the oldest.'
            }
          >
            <span className="tnum cursor-default font-semibold text-foreground">
              {Math.round(row.original.priority_score).toLocaleString('en-IN')}
            </span>
          </Tooltip>
        ),
      },
      {
        accessorKey: 'expected_recovery',
        header: 'Recoverable',
        cell: ({ row }) => {
          const value = row.original.expected_recovery ?? 0
          const band = priorityBand(value)
          return (
            <Tooltip content={band.hint}>
              <span className="inline-flex cursor-default items-center gap-2">
                <Badge tone={band.tone}>{band.label}</Badge>
                <span className="tnum text-xs text-muted">
                  {value > 0 ? compactCurrency(value, row.original.currency) : '—'}
                </span>
              </span>
            </Tooltip>
          )
        },
      },
      {
        accessorKey: 'vertical',
        header: 'Vertical',
        cell: ({ row }) => (
          <Badge tone="accent">{VERTICAL_LABELS[row.original.vertical] ?? row.original.vertical}</Badge>
        ),
      },
      {
        accessorKey: 'amount',
        header: 'At risk',
        cell: ({ row }) => (
          <span className="tnum font-medium">
            {currency(row.original.amount, row.original.currency)}
          </span>
        ),
      },
      {
        accessorKey: 'customer_id',
        header: 'Customer',
        cell: ({ row }) => (
          <span className="font-mono text-xs text-muted">{row.original.customer_id}</span>
        ),
      },
      {
        accessorKey: 'diagnosis',
        header: 'Diagnosis',
        cell: ({ row }) => <span className="text-sm">{humanise(row.original.diagnosis)}</span>,
      },
      {
        accessorKey: 'latest_confidence',
        header: 'Confidence',
        cell: ({ row }) => {
          const value = row.original.latest_confidence
          if (value === null) return <span className="text-xs text-subtle">—</span>
          return (
            <Badge tone={confidenceTone(value)}>{percent(value)}</Badge>
          )
        },
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => (
          <Badge tone={caseStatusTone(row.original.status)}>
            {CASE_STATUS_LABELS[row.original.status] ?? row.original.status}
          </Badge>
        ),
      },
      {
        accessorKey: 'step_count',
        header: 'Steps',
        cell: ({ row }) => <span className="tnum text-sm text-muted">{row.original.step_count}</span>,
      },
      {
        accessorKey: 'updated_at',
        header: 'Updated',
        cell: ({ row }) => (
          <span className="whitespace-nowrap text-xs text-muted">
            {relativeTime(row.original.updated_at)}
          </span>
        ),
      },
    ],
    [scoreMeaning],
  )

  const table = useReactTable({
    data: cases,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getRowId: (row) => row.id,
  })

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id} className="border-b border-border">
              {headerGroup.headers.map((header) => {
                const sorted = header.column.getIsSorted()
                return (
                  <th
                    key={header.id}
                    onClick={header.column.getToggleSortingHandler()}
                    className="cursor-pointer select-none px-4 py-2.5 text-left text-[0.68rem] font-semibold uppercase tracking-[0.05em] text-muted hover:text-foreground"
                  >
                    <span className="inline-flex items-center gap-1">
                      {flexRender(header.column.columnDef.header, header.getContext())}
                      {sorted === 'asc' ? (
                        <ArrowUp size={12} />
                      ) : sorted === 'desc' ? (
                        <ArrowDown size={12} />
                      ) : (
                        <ChevronsUpDown size={12} className="opacity-30" />
                      )}
                    </span>
                  </th>
                )
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          <AnimatePresence initial={false}>
            {table.getRowModel().rows.map((row) => (
              <motion.tr
                key={row.id}
                layout
                initial={{ opacity: 0, backgroundColor: 'rgba(99,102,241,0.14)' }}
                animate={{ opacity: 1, backgroundColor: 'rgba(99,102,241,0)' }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.45 }}
                onClick={() => navigate(`/cases/${row.original.id}`)}
                className={cn(
                  'cursor-pointer border-b border-border/60 transition-colors hover:bg-surface-hover',
                )}
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-4 py-3">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </motion.tr>
            ))}
          </AnimatePresence>
        </tbody>
      </table>
    </div>
  )
}
