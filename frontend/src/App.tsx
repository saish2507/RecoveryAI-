import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Suspense, lazy } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from '@/components/AppShell'
import { Skeleton, TooltipProvider } from '@/components/ui'
import { WsProvider } from '@/lib/ws'

// Routes are lazy so the initial download is the shell plus one page, not the
// whole console. The trace timeline in particular drags in animation and table
// code that the dashboard never touches.
const Dashboard = lazy(() => import('@/pages/Dashboard').then((m) => ({ default: m.Dashboard })))
const CaseQueue = lazy(() => import('@/pages/CaseQueue').then((m) => ({ default: m.CaseQueue })))
const CaseDetail = lazy(() => import('@/pages/CaseDetail').then((m) => ({ default: m.CaseDetail })))
const ReviewQueue = lazy(() => import('@/pages/ReviewQueue').then((m) => ({ default: m.ReviewQueue })))
const DevTools = lazy(() => import('@/pages/DevTools').then((m) => ({ default: m.DevTools })))

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // The WebSocket drives freshness, so polling would be redundant work.
      // Data still refetches when a tab regains focus, in case a push was
      // missed while the socket was down.
      refetchOnWindowFocus: true,
      staleTime: 5_000,
      retry: 1,
    },
  },
})

/** Shown while a route chunk downloads. Shaped like a page so nothing jumps. */
function RouteFallback() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-8 w-56" />
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} className="h-24" />
        ))}
      </div>
      <Skeleton className="h-72" />
    </div>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <WsProvider>
        <TooltipProvider>
          <BrowserRouter>
            <AppShell>
              <Suspense fallback={<RouteFallback />}>
                <Routes>
                  <Route path="/" element={<Dashboard />} />
                  <Route path="/cases" element={<CaseQueue />} />
                  <Route path="/cases/:caseId" element={<CaseDetail />} />
                  <Route path="/review" element={<ReviewQueue />} />
                  <Route path="/dev" element={<DevTools />} />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>
              </Suspense>
            </AppShell>
          </BrowserRouter>
        </TooltipProvider>
      </WsProvider>
    </QueryClientProvider>
  )
}
