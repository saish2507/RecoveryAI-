/** The work queue — priority-ordered, filterable, paginated. */

import { Inbox } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { CaseTable } from '@/components/CaseTable'
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  EmptyState,
  ErrorState,
  Select,
  Skeleton,
} from '@/components/ui'
import { useCases } from '@/lib/api'
import type { Vertical } from '@/lib/types'

const PAGE_SIZE = 25

export function CaseQueue() {
  const [vertical, setVertical] = useState<Vertical | ''>('')
  const [status, setStatus] = useState('')
  const [offset, setOffset] = useState(0)

  const query = useCases({
    vertical: vertical || undefined,
    status: status || undefined,
    limit: PAGE_SIZE,
    offset,
  })

  const reset = <T,>(setter: (value: T) => void) => (value: T) => {
    setter(value)
    setOffset(0) // a filter change invalidates the current page position
  }

  const total = query.data?.total ?? 0
  const shown = query.data?.items.length ?? 0
  const hasPrev = offset > 0
  const hasNext = offset + shown < total

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="text-lg font-bold">Case Queue</h1>
          <p className="text-xs text-muted">
            Ordered by priority, not arrival. Undecided cases rank on{' '}
            <span className="font-mono">amount × urgency</span>, so the largest at-risk revenue is
            worked first.
          </p>
        </div>

        <div className="ml-auto flex items-center gap-2">
          <Select value={vertical} onChange={(e) => reset(setVertical)(e.target.value as Vertical | '')}>
            <option value="">All verticals</option>
            <option value="cart">Checkout</option>
            <option value="b2b">Receivables</option>
            <option value="autopay">Autopay</option>
          </Select>
          <Select value={status} onChange={(e) => reset(setStatus)(e.target.value)}>
            <option value="">All statuses</option>
            <option value="new">New</option>
            <option value="in_progress">In progress</option>
            <option value="escalated">Escalated</option>
            <option value="resolved">Resolved</option>
            <option value="abandoned">Closed</option>
          </Select>
        </div>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>
            {total} case{total === 1 ? '' : 's'}
            {shown < total ? ` · showing ${offset + 1}–${offset + shown}` : ''}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {query.isError ? (
            <ErrorState
              message={(query.error as Error).message}
              onRetry={() => void query.refetch()}
            />
          ) : query.isLoading ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 6 }).map((_, index) => (
                <Skeleton key={index} className="h-10" />
              ))}
            </div>
          ) : shown === 0 ? (
            <EmptyState
              icon={<Inbox size={26} />}
              title={vertical || status ? 'No cases match these filters' : 'No cases yet'}
              description={
                vertical || status
                  ? 'Try widening the filters.'
                  : 'Cases appear as events arrive. Inject one from Dev Tools, or POST to /api/v1/events.'
              }
              action={
                vertical || status ? (
                  <Button
                    size="sm"
                    onClick={() => {
                      setVertical('')
                      setStatus('')
                      setOffset(0)
                    }}
                  >
                    Clear filters
                  </Button>
                ) : (
                  <Button asChild size="sm" variant="primary">
                    <Link to="/dev">Open Dev Tools</Link>
                  </Button>
                )
              }
            />
          ) : (
            <CaseTable cases={query.data!.items} scoreMeaning="intake" />
          )}
        </CardContent>
      </Card>

      {(hasPrev || hasNext) && (
        <div className="flex items-center justify-end gap-2">
          <Button
            size="sm"
            disabled={!hasPrev}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            Previous
          </Button>
          <Button size="sm" disabled={!hasNext} onClick={() => setOffset(offset + PAGE_SIZE)}>
            Next
          </Button>
        </div>
      )}
    </div>
  )
}
