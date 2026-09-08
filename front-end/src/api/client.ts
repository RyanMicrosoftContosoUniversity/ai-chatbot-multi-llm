import { ApiError, StreamError } from './errors'
import { consumeChatStream } from './sse'
import { isRecord, readConversation, readMessage, readModels, readPage } from './validation'
import type { ChatApi } from './types'

export type TokenProvider = () => Promise<string>
export type Fetcher = typeof fetch

async function httpError(response: Response): Promise<ApiError> {
  const text = await response.text()
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error
    // HTML ingress/proxy errors must not be rendered as trusted API messages.
  }
  const problem = isRecord(value) && isRecord(value.error) ? value.error : undefined
  const retryHeader = response.headers.get('Retry-After')
  const retrySeconds = retryHeader === null ? undefined
    : /^\d+$/.test(retryHeader) ? Number(retryHeader)
      : Math.max(0, Math.ceil((Date.parse(retryHeader) - Date.now()) / 1000))
  const retryAfter = typeof problem?.retryAfter === 'number' ? problem.retryAfter : retrySeconds
  return new ApiError({
    code: typeof problem?.code === 'string' ? problem.code : `http_${response.status}`,
    message: typeof problem?.message === 'string' ? problem.message : `The API returned HTTP ${response.status}. No request was retried.`,
    requestId: typeof problem?.requestId === 'string' ? problem.requestId : response.headers.get('X-Request-ID') ?? undefined,
    retryAfter: retryAfter !== undefined && Number.isFinite(retryAfter) && retryAfter >= 0 ? retryAfter : undefined,
  }, response.status)
}

export function createApi(baseUrl: string, getToken: TokenProvider, fetcher: Fetcher = fetch): ChatApi {
  const root = `${baseUrl.replace(/\/+$/, '')}/api/v1`
  const segment = encodeURIComponent
  async function request(path: string, signal: AbortSignal, method = 'GET', body?: object): Promise<Response> {
    signal.throwIfAborted()
    const token = await getToken()
    signal.throwIfAborted()
    if (!token) throw new ApiError({ code: 'authentication_required', message: 'Sign in again to acquire an API access token.' })
    const response = await fetcher(`${root}${path}`, {
      method, signal, mode: 'cors', credentials: 'omit', cache: 'no-store', redirect: 'error',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: method === 'POST' && (path === '/chat' || path.endsWith('/messages')) ? 'text/event-stream' : 'application/json',
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    })
    if (!response.ok) throw await httpError(response)
    return response
  }
  async function json(path: string, signal: AbortSignal, method?: string, body?: object): Promise<unknown> {
    const response = await request(path, signal, method, body)
    return response.json()
  }
  function pageQuery(limit: number, token?: string): string {
    const params = new URLSearchParams({ limit: String(limit) })
    if (token) params.set('continuationToken', token)
    return `?${params.toString()}`
  }
  return {
    models: async (signal) => readModels(await json('/models', signal)),
    conversations: async (signal, token) =>
      readPage(await json(`/conversations${pageQuery(20, token)}`, signal), readConversation),
    createConversation: async (title, signal) => readConversation(await json('/conversations', signal, 'POST', { title })),
    renameConversation: async (id, title, signal) =>
      readConversation(await json(`/conversations/${segment(id)}`, signal, 'PATCH', { title })),
    deleteConversation: async (id, signal) => { await request(`/conversations/${segment(id)}`, signal, 'DELETE') },
    messages: async (id, signal, token) =>
      readPage(await json(`/conversations/${segment(id)}/messages${pageQuery(50, token)}`, signal), readMessage),
    feedback: async (id, messageId, rating, signal) => {
      await request(`/conversations/${segment(id)}/messages/${segment(messageId)}/feedback`, signal, 'PUT', { rating })
    },
    stream: async (input, signal, onEvent, conversation) => {
      const path = conversation ? `/conversations/${segment(conversation.id)}/messages` : '/chat'
      const body = conversation ? { ...input, clientMessageId: conversation.clientMessageId } : input
      const response = await request(path, signal, 'POST', body)
      if (!response.headers.get('Content-Type')?.toLowerCase().startsWith('text/event-stream')) {
        await response.body?.cancel()
        throw new StreamError('The API did not return a streaming response.')
      }
      if (!response.body) throw new StreamError('This browser could not read the response stream.')
      await consumeChatStream(response.body, signal, onEvent)
    },
  }
}
