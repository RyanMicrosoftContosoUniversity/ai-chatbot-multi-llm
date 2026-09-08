// @vitest-environment node
import { describe, expect, it, vi } from 'vitest'
import { createApi } from './client'

const getToken = () => Promise.resolve('unit-test-token')
const input = { model: 'luna', message: 'Hello' }
const terminal = 'event: meta\ndata: {"version":"1","requestId":"r1","model":"luna","remainingQuota":null}\n\nevent: done\ndata: {"status":"completed","messageId":"m1"}\n\n'

describe('authenticated API client', () => {
  it('sends only the API bearer token and explicit chat input to the configured cross-origin endpoint', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(terminal, { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } }))
    const api = createApi('https://api.example.test/', getToken, fetcher)
    await api.stream(input, new AbortController().signal, () => {})
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher).toHaveBeenCalledWith('https://api.example.test/api/v1/chat', expect.objectContaining({
      method: 'POST', credentials: 'omit', mode: 'cors', redirect: 'error',
      body: JSON.stringify(input),
      headers: expect.objectContaining({ Authorization: 'Bearer unit-test-token', 'Content-Type': 'application/json', Accept: 'text/event-stream' }),
    }))
  })

  it('surfaces rate-limit metadata without automatically retrying inference', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      error: { code: 'rate_limited', message: 'Slow down' },
    }), { status: 429, headers: { 'Retry-After': '12', 'X-Request-ID': 'request-429' } }))
    const api = createApi('https://api.example.test', getToken, fetcher)
    await expect(api.stream(input, new AbortController().signal, () => {})).rejects.toMatchObject({
      code: 'rate_limited', status: 429, retryAfter: 12, requestId: 'request-429',
    })
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('does not relabel every 403 as quota exhaustion or render HTML errors', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response('<script>bad()</script>', { status: 403 }))
    await expect(createApi('https://api.example.test', getToken, fetcher).models(new AbortController().signal))
      .rejects.toMatchObject({ code: 'http_403', message: 'The API returned HTTP 403. No request was retried.' })
  })

  it('passes abort through and does not fetch after a cancelled token acquisition', async () => {
    let resolveToken: (value: string) => void = () => {}
    const token = new Promise<string>((resolve) => { resolveToken = resolve })
    const fetcher = vi.fn<typeof fetch>()
    const abort = new AbortController()
    const result = createApi('https://api.example.test', () => token, fetcher).models(abort.signal)
    abort.abort()
    resolveToken('unit-test-token')
    await expect(result).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('propagates fetch abort without replay', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new DOMException('Aborted', 'AbortError'))
    await expect(createApi('https://api.example.test', getToken, fetcher).stream(input, new AbortController().signal, () => {}))
      .rejects.toMatchObject({ name: 'AbortError' })
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('uses a durable clientMessageId without sending a client transcript and URL-encodes pagination', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(terminal, { headers: { 'Content-Type': 'text/event-stream' } }))
      .mockResolvedValueOnce(Response.json({ items: [], continuationToken: null }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    const api = createApi('https://api.example.test', getToken, fetcher)
    const signal = new AbortController().signal
    await api.stream(input, signal, () => {}, { id: 'c/1', clientMessageId: 'c34bfaad-848e-4d20-8ddc-39fde4f8c0a0' })
    expect(fetcher.mock.calls[0][0]).toBe('https://api.example.test/api/v1/conversations/c%2F1/messages')
    expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual({ ...input, clientMessageId: 'c34bfaad-848e-4d20-8ddc-39fde4f8c0a0' })
    await api.messages('c/1', signal, 'opaque+/= token')
    expect(fetcher.mock.calls[1][0]).toContain('continuationToken=opaque%2B%2F%3D+token')
    await api.feedback('c/1', 'm/1', null, signal)
    expect(fetcher.mock.calls[2][1]).toMatchObject({ method: 'PUT', body: '{"rating":null}' })
  })

  it('rejects a successful HTTP response with a non-SSE body and invalid discovery', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(Response.json({ text: 'not an SSE stream' }))
      .mockResolvedValueOnce(Response.json({ models: [{ id: 4 }], historyEnabled: false }))
    const api = createApi('https://api.example.test', getToken, fetcher)
    await expect(api.stream(input, new AbortController().signal, () => {})).rejects.toMatchObject({ code: 'invalid_stream' })
    await expect(api.models(new AbortController().signal)).rejects.toMatchObject({ code: 'invalid_response' })
  })
})
