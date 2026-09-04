/**
 * The model switch is the one header control that spends money when it is on,
 * so its three states are pinned: on (with budget), off, and nothing-to-switch.
 *
 * Driven through the real query hooks against a stubbed `fetch` rather than
 * mocking `@/lib/api`, because the part most likely to break is the wiring —
 * the request body, and whether the status cache actually picks up the reply.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LlmSwitch } from './AppShell'
import { TooltipProvider } from '@/components/ui'
import type { SystemStatus } from '@/lib/types'

type Llm = SystemStatus['agent']['llm']

function llmState(overrides: Partial<Llm> = {}): Llm {
  return {
    provider: 'gemini',
    configured: true,
    enabled: true,
    available: true,
    cache_entries: 0,
    minute_tokens_remaining: 4,
    minute_capacity: 4,
    day_tokens_remaining: 74,
    day_capacity: 90,
    ...overrides,
  }
}

/** A server that holds the switch state, so a POST is visible to the next GET. */
function stubServer(initial: Llm) {
  let current = initial
  const posts: unknown[] = []

  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as { enabled: boolean }
      posts.push(body)
      current = { ...current, enabled: body.enabled, available: body.enabled && current.configured }
      return { ok: true, status: 200, json: async () => current }
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ agent: { llm: current } }),
    }
  })

  vi.stubGlobal('fetch', fetchMock)
  return { posts }
}

function renderSwitch() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <LlmSwitch />
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('LlmSwitch', () => {
  it('shows the remaining daily budget while the model is on', async () => {
    stubServer(llmState())
    renderSwitch()

    const control = await screen.findByRole('switch')
    expect(control).toHaveTextContent('LLM 74/90')
    expect(control).toHaveAttribute('aria-checked', 'true')
    expect(control).not.toBeDisabled()
  })

  it('posts the new state and flips to rules-only when switched off', async () => {
    const { posts } = stubServer(llmState())
    renderSwitch()

    fireEvent.click(await screen.findByRole('switch'))

    await waitFor(() => expect(posts).toEqual([{ enabled: false }]))
    await waitFor(() => {
      const control = screen.getByRole('switch')
      expect(control).toHaveTextContent('Rules only')
      expect(control).toHaveAttribute('aria-checked', 'false')
    })
  })

  it('switches back on from the off state', async () => {
    const { posts } = stubServer(llmState({ enabled: false, available: false }))
    renderSwitch()

    const control = await screen.findByRole('switch')
    expect(control).toHaveTextContent('Rules only')

    fireEvent.click(control)

    await waitFor(() => expect(posts).toEqual([{ enabled: true }]))
    await waitFor(() => expect(screen.getByRole('switch')).toHaveTextContent('LLM 74/90'))
  })

  it('offers no control when there is no key to switch', async () => {
    const { posts } = stubServer(llmState({ configured: false, available: false }))
    renderSwitch()

    const control = await screen.findByRole('switch')
    expect(control).toHaveTextContent('No LLM key')
    expect(control).toBeDisabled()

    fireEvent.click(control)
    expect(posts).toEqual([])
  })
})
