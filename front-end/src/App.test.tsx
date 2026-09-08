import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import App, { SetupState } from './App'
import { ApiError } from './api/errors'
import type { ChatApi, Conversation, Message } from './api/types'
import type { AuthSession } from './auth/auth'

const conversation: Conversation = { id: 'c1', title: 'Saved research', createdAt: '2026-09-01T00:00:00Z', updatedAt: '2026-09-01T00:00:00Z' }
const savedMessage: Message = {
  id: 'm1', conversationId: 'c1', role: 'assistant', content: 'A saved response', status: 'completed',
  model: 'luna', createdAt: '2026-09-01T00:00:00Z',
  usage: { promptTokens: 12, completionTokens: 24, source: 'provider' }, feedback: 'up',
}
function makeAuth(changes: Partial<AuthSession> = {}): AuthSession {
  return {
    account: { id: 'a1', name: 'Test User', username: 'user@example.test' }, accounts: [],
    busy: false, error: null, needsInteraction: false,
    signIn: vi.fn().mockResolvedValue(undefined), signOut: vi.fn().mockResolvedValue(undefined),
    reconnect: vi.fn().mockResolvedValue(undefined), selectAccount: vi.fn(),
    getToken: vi.fn().mockResolvedValue('test-only'),
    ...changes,
  }
}
function makeApi(changes: Partial<ChatApi> = {}): ChatApi {
  return {
    models: vi.fn().mockResolvedValue({ models: [{ id: 'luna' }, { id: 'deepseek' }], historyEnabled: false }),
    conversations: vi.fn().mockResolvedValue({ items: [], continuationToken: null }),
    createConversation: vi.fn().mockResolvedValue(conversation),
    renameConversation: vi.fn().mockResolvedValue({ ...conversation, title: 'Renamed research' }),
    deleteConversation: vi.fn().mockResolvedValue(undefined),
    messages: vi.fn().mockResolvedValue({ items: [savedMessage], continuationToken: null }),
    feedback: vi.fn().mockResolvedValue(undefined),
    stream: vi.fn<ChatApi['stream']>().mockImplementation(async (_input, _signal, onEvent) => {
      onEvent({ type: 'meta', data: { version: '1', requestId: 'r1', model: 'luna', remainingQuota: 1234 } })
      onEvent({ type: 'delta', data: { text: '<script>alert("not executable")</script>' } })
      onEvent({ type: 'done', data: { status: 'completed', messageId: null } })
    }),
    ...changes,
  }
}
async function ready() {
  await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('luna'))
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled())
}
async function send(text = 'Hello') {
  const user = userEvent.setup()
  await user.type(screen.getByRole('textbox', { name: 'Message' }), text)
  await user.click(screen.getByRole('button', { name: 'Send message' }))
}

describe('workspace user states', () => {
  it('gates all API calls behind sign-in and surfaces setup errors without an auth bypass', async () => {
    const api = makeApi()
    const auth = makeAuth({ account: null, error: 'Consent was declined.' })
    const view = render(<App api={api} auth={auth} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Consent was declined.')
    await userEvent.click(screen.getByRole('button', { name: /Sign in with Microsoft/ }))
    expect(auth.signIn).toHaveBeenCalledOnce()
    expect(api.models).not.toHaveBeenCalled()
    view.unmount()
    render(<SetupState message="Missing public setting: VITE_API_BASE_URL." />)
    expect(screen.getByRole('alert')).toHaveTextContent('Missing public setting')
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('discovers only server aliases and renders model output as text with honest usage and quota', async () => {
    const api = makeApi()
    const { container } = render(<App api={api} auth={makeAuth()} />)
    await ready()
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual(['luna', 'deepseek'])
    expect(screen.getByText('No conversation memory is sent.')).toBeInTheDocument()
    await send()
    expect(await screen.findByText('<script>alert("not executable")</script>')).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
    expect(screen.getByText('Usage unavailable')).toBeInTheDocument()
    expect(screen.getByText(/Approx. remaining quota: 1,234/)).toBeInTheDocument()
    expect(screen.getByText('Gateway snapshot, not an exact balance or billing statement.')).toBeInTheDocument()
    expect(api.stream).toHaveBeenCalledWith({ model: 'luna', message: 'Hello' }, expect.any(AbortSignal), expect.any(Function), undefined)
    expect(api.conversations).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Helpful response' })).not.toBeInTheDocument()
  })

  it('supports Enter send, Shift+Enter newline and an explicit model choice', async () => {
    const api = makeApi()
    render(<App api={api} auth={makeAuth()} />)
    await ready()
    const user = userEvent.setup()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Model' }), 'deepseek')
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'First{Shift>}{Enter}{/Shift}Second')
    expect(api.stream).not.toHaveBeenCalled()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(api.stream).toHaveBeenCalledOnce())
    expect(api.stream).toHaveBeenCalledWith({ model: 'deepseek', message: 'First\nSecond' }, expect.any(AbortSignal), expect.any(Function), undefined)
  })

  it('shows missing-terminal responses as interrupted and never retries inference', async () => {
    const api = makeApi({ stream: vi.fn<ChatApi['stream']>().mockImplementation(async (_input, _signal, onEvent) => {
      onEvent({ type: 'delta', data: { text: 'Partial text' } })
    }) })
    render(<App api={api} auth={makeAuth()} />)
    await ready()
    await send()
    expect(await screen.findByText('Interrupted')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('without a completion event')
    expect(screen.getByText('Usage unavailable')).toBeInTheDocument()
    expect(api.stream).toHaveBeenCalledOnce()
  })

  it('shows failed done and recognized rate metadata separately from success', async () => {
    const api = makeApi({ stream: vi.fn<ChatApi['stream']>().mockImplementation(async (_input, _signal, onEvent) => {
      onEvent({ type: 'error', data: { code: 'rate_limited', message: 'Token rate exhausted', retryAfter: 10, requestId: 'r2' } })
      onEvent({ type: 'done', data: { status: 'failed', messageId: null } })
    }) })
    render(<App api={api} auth={makeAuth()} />)
    await ready()
    await send()
    expect(await screen.findByText('Failed')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('Wait at least 10 seconds')
    expect(screen.getByRole('alert')).toHaveTextContent('Request ID: r2')
    expect(screen.queryByText('Completed')).not.toBeInTheDocument()
    expect(api.stream).toHaveBeenCalledOnce()
  })

  it('stops an in-flight request, preserves partial output, and explains best-effort cancellation', async () => {
    let signal: AbortSignal | undefined
    const api = makeApi({ stream: vi.fn<ChatApi['stream']>().mockImplementation(async (_input, currentSignal, onEvent) => {
      signal = currentSignal
      onEvent({ type: 'delta', data: { text: 'Partial response' } })
      await new Promise<void>((_resolve, reject) => currentSignal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true }))
    }) })
    render(<App api={api} auth={makeAuth()} />)
    await ready()
    await send()
    expect(await screen.findByRole('button', { name: 'Stop' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Stop' }))
    expect(await screen.findByText('Stopped locally')).toBeInTheDocument()
    expect(signal?.aborted).toBe(true)
    expect(screen.getByText('Partial response')).toBeInTheDocument()
    expect(screen.getByText(/Provider cancellation is best-effort/)).toBeInTheDocument()
    expect(api.stream).toHaveBeenCalledOnce()
  })

  it('aborts and clears account-scoped state on account change and unmount', async () => {
    let signal: AbortSignal | undefined
    const api = makeApi({ stream: vi.fn<ChatApi['stream']>().mockImplementation(async (_input, currentSignal, onEvent) => {
      signal = currentSignal
      onEvent({ type: 'delta', data: { text: 'Private response' } })
      await new Promise<void>((_resolve, reject) => currentSignal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true }))
    }) })
    const view = render(<App api={api} auth={makeAuth()} />)
    await ready()
    await send()
    await screen.findByText('Private response')
    view.rerender(<App api={api} auth={makeAuth({ account: { id: 'a2', name: 'Another user', username: 'other@example.test' } })} />)
    expect(signal?.aborted).toBe(true)
    expect(screen.queryByText('Private response')).not.toBeInTheDocument()
    await ready()
    await send('Second request')
    await screen.findByRole('button', { name: 'Stop' })
    view.unmount()
    expect(signal?.aborted).toBe(true)
  })

  it('handles unavailable model discovery and exposes an explicit safe refresh', async () => {
    const models = vi.fn<ChatApi['models']>().mockRejectedValueOnce(new ApiError({ code: 'forbidden', message: 'API permission required.' }, 403))
      .mockResolvedValue({ models: [], historyEnabled: false })
    render(<App api={makeApi({ models })} auth={makeAuth()} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('API permission required.')
    expect(screen.queryByText('Token quota reached')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Refresh models' }))
    expect(await screen.findByText(/No allowed model aliases are currently available/)).toBeInTheDocument()
    expect(models).toHaveBeenCalledTimes(2)
  })

  it('loads paginated saved messages and restores feedback after remount', async () => {
    const api = makeApi({
      models: vi.fn().mockResolvedValue({ models: [{ id: 'luna' }], historyEnabled: true }),
      conversations: vi.fn().mockResolvedValue({ items: [conversation], continuationToken: 'next-list' }),
      messages: vi.fn<ChatApi['messages']>()
        .mockResolvedValueOnce({ items: [savedMessage], continuationToken: 'next-message' })
        .mockResolvedValue({ items: [{ ...savedMessage, id: 'm2', content: 'Another saved response', feedback: null }], continuationToken: null }),
    })
    const view = render(<App api={api} auth={makeAuth()} />)
    await userEvent.click(await screen.findByRole('button', { name: /Saved research/ }))
    expect(await screen.findByText('A saved response')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Helpful response' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Load more messages' }))
    expect(await screen.findByText('Another saved response')).toBeInTheDocument()
    expect(api.messages).toHaveBeenLastCalledWith('c1', expect.any(AbortSignal), 'next-message')
    const first = screen.getAllByRole('article')[0]
    await userEvent.click(within(first).getByRole('button', { name: 'Helpful response' }))
    expect(api.feedback).toHaveBeenLastCalledWith('c1', 'm1', null, expect.any(AbortSignal))
    await userEvent.click(within(first).getByRole('button', { name: 'Unhelpful response' }))
    expect(api.feedback).toHaveBeenLastCalledWith('c1', 'm1', 'down', expect.any(AbortSignal))
    await userEvent.click(screen.getByRole('button', { name: 'Load more conversations' }))
    expect(api.conversations).toHaveBeenLastCalledWith(expect.any(AbortSignal), 'next-list')
    view.unmount()
    render(<App api={api} auth={makeAuth()} />)
    await userEvent.click(await screen.findByRole('button', { name: /Saved research/ }))
    expect(await screen.findByText('Another saved response')).toBeInTheDocument()
  })

  it('creates a durable conversation with UUID, renames it, and deletes only on confirmation', async () => {
    const api = makeApi({
      models: vi.fn().mockResolvedValue({ models: [{ id: 'luna' }], historyEnabled: true }),
      stream: vi.fn<ChatApi['stream']>().mockImplementation(async (_input, _signal, onEvent) => {
        onEvent({ type: 'delta', data: { text: 'Persisted completion' } })
        onEvent({ type: 'done', data: { status: 'completed', messageId: 'm2' } })
      }),
    })
    render(<App api={api} auth={makeAuth()} />)
    await ready()
    await send('A new idea')
    expect(await screen.findByText('Persisted completion')).toBeInTheDocument()
    expect(api.createConversation).toHaveBeenCalledWith('A new idea', expect.any(AbortSignal))
    expect(api.stream).toHaveBeenCalledWith({ model: 'luna', message: 'A new idea' }, expect.any(AbortSignal), expect.any(Function),
      { id: 'c1', clientMessageId: expect.stringMatching(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/) })
    expect(screen.getByRole('button', { name: 'Helpful response' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Rename' }))
    const title = screen.getByRole('textbox', { name: 'Conversation name' })
    await userEvent.clear(title)
    await userEvent.type(title, 'Renamed research')
    await userEvent.click(screen.getByRole('button', { name: 'Save name' }))
    expect(api.renameConversation).toHaveBeenCalledWith('c1', 'Renamed research', expect.any(AbortSignal))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(api.deleteConversation).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Delete conversation' }))
    await waitFor(() => expect(api.deleteConversation).toHaveBeenCalledWith('c1', expect.any(AbortSignal)))
    expect(screen.queryByText('Persisted completion')).not.toBeInTheDocument()
  })

  it('does not replace a user-cancelled view with late history responses', async () => {
    let resolveMessages: (page: { items: Message[]; continuationToken: null }) => void = () => {}
    const api = makeApi({
      models: vi.fn().mockResolvedValue({ models: [{ id: 'luna' }], historyEnabled: true }),
      conversations: vi.fn().mockResolvedValue({ items: [conversation], continuationToken: null }),
      messages: vi.fn().mockImplementation(() => new Promise((resolve) => { resolveMessages = resolve })),
    })
    render(<App api={api} auth={makeAuth()} />)
    await userEvent.click(await screen.findByRole('button', { name: /Saved research/ }))
    await userEvent.click(screen.getByRole('button', { name: /New conversation/ }))
    await act(async () => { resolveMessages({ items: [savedMessage], continuationToken: null }) })
    expect(screen.queryByText('A saved response')).not.toBeInTheDocument()
  })

  it('blocks sending after a failed history read until an explicit refresh succeeds', async () => {
    const messages = vi.fn<ChatApi['messages']>().mockRejectedValueOnce(new ApiError({ code: 'unavailable', message: 'History is unavailable.' }))
      .mockResolvedValue({ items: [savedMessage], continuationToken: null })
    const api = makeApi({
      models: vi.fn().mockResolvedValue({ models: [{ id: 'luna' }], historyEnabled: true }),
      conversations: vi.fn().mockResolvedValue({ items: [conversation], continuationToken: null }),
      messages,
    })
    render(<App api={api} auth={makeAuth()} />)
    await userEvent.click(await screen.findByRole('button', { name: /Saved research/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('History is unavailable.')
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Refresh history' }))
    expect(await screen.findByText('A saved response')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled()
    expect(api.stream).not.toHaveBeenCalled()
  })

  it('preserves the saved feedback on a failed update and allows retrying a failed list read', async () => {
    const conversations = vi.fn<ChatApi['conversations']>().mockRejectedValueOnce(new ApiError({ code: 'unavailable', message: 'List unavailable.' }))
      .mockResolvedValue({ items: [conversation], continuationToken: null })
    const api = makeApi({
      models: vi.fn().mockResolvedValue({ models: [{ id: 'luna' }], historyEnabled: true }),
      conversations,
      feedback: vi.fn().mockRejectedValue(new ApiError({ code: 'unavailable', message: 'Feedback could not be saved.' })),
    })
    render(<App api={api} auth={makeAuth()} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('List unavailable.')
    await userEvent.click(screen.getByRole('button', { name: 'Refresh conversations' }))
    await userEvent.click(await screen.findByRole('button', { name: /Saved research/ }))
    await screen.findByText('A saved response')
    await userEvent.click(screen.getByRole('button', { name: 'Unhelpful response' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Feedback could not be saved.')
    expect(screen.getByRole('button', { name: 'Helpful response' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Unhelpful response' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('shows a persisted pending result as unfinalized, not a live or completed generation', async () => {
    const api = makeApi({
      models: vi.fn().mockResolvedValue({ models: [{ id: 'luna' }], historyEnabled: true }),
      conversations: vi.fn().mockResolvedValue({ items: [conversation], continuationToken: null }),
      messages: vi.fn().mockResolvedValue({ items: [{ ...savedMessage, status: 'pending', usage: undefined }], continuationToken: null }),
    })
    render(<App api={api} auth={makeAuth()} />)
    await userEvent.click(await screen.findByRole('button', { name: /Saved research/ }))
    expect(await screen.findByText('Unfinalized')).toBeInTheDocument()
    expect(screen.getByText(/This saved request has not been finalized/)).toBeInTheDocument()
    expect(screen.getByText('Usage unavailable')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument()
    expect(api.stream).not.toHaveBeenCalled()
  })
})
