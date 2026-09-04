/**
 * Human review queue.
 *
 * The ordering is the feature. Sorting escalations by arrival time makes a
 * reviewer's first hour indistinguishable from a random sample; sorting by
 * `(1 − confidence) × amount` puts the expensive uncertainty first.
 */

import { CheckCircle2, Info } from 'lucide-react'
import { useState } from 'react'
import { CaseTable } from '@/components/CaseTable'
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  EmptyState,
  ErrorState,
  Skeleton,
  Tabs,
  TabsList,
  TabsTrigger,
} from '@/components/ui'
import { useReviewQueue } from '@/lib/api'
import { compactCurrency } from '@/lib/format'

const PAGE_SIZE = 25

export function ReviewQueue() {
  const [include, setInclude] = useState<'escalated' | 'all_decided'>('escalated')
  const [offset, setOffset] = useState(0)

  const query = useReviewQueue({ include, limit: PAGE_SIZE, offset })

  const total = query.data?.total ?? 0
  const shown = query.data?.items.length ?? 0
  const exposure = (query.data?.items ?? []).reduce((sum, item) => sum + item.amount, 0)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="text-lg font-bold">Human Review</h1>
          <p className="text-xs text-muted">
            Ordered by <span className="font-mono">(1 − confidence) × amount</span> — the largest
            decisions the agent was least sure about come first.
          </p>
        </div>

        <Tabs
          value={include}
          onValueChange={(value) => {
            setInclude(value as 'escalated' | 'all_decided')
            setOffset(0)
          }}
          className="ml-auto"
        >
          <TabsList>
            <TabsTrigger value="escalated">Escalated</TabsTrigger>
            <TabsTrigger value="all_decided">All decided</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      <Card className="border-accent/30">
        <CardContent className="flex items-start gap-3 text-xs leading-relaxed text-muted">
          <Info size={15} className="mt-0.5 shrink-0 text-accent-hover" />
          <p>
            A case reaches this queue for one of three reasons: the agent chose to escalate, a
            guardrail exhausted every automated lever, or the workflow hit its step limit. The
            confidence column tells them apart — a high-confidence escalation is a policy outcome,
            a low-confidence one is a genuine judgement call worth your attention.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>
            {total} awaiting review
            {exposure > 0 ? ` · ${compactCurrency(exposure)} on this page` : ''}
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
              {Array.from({ length: 5 }).map((_, index) => (
                <Skeleton key={index} className="h-10" />
              ))}
            </div>
          ) : shown === 0 ? (
            <EmptyState
              icon={<CheckCircle2 size={26} className="text-success" />}
              title="Nothing needs a human"
              description="Every case so far was resolved inside the agent's guardrails. Escalations land here as they happen."
            />
          ) : (
            <CaseTable cases={query.data!.items} scoreMeaning="review" />
          )}
        </CardContent>
      </Card>

      {(offset > 0 || offset + shown < total) && (
        <div className="flex items-center justify-end gap-2">
          <Button
            size="sm"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            Previous
          </Button>
          <Button
            size="sm"
            disabled={offset + shown >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  )
}
