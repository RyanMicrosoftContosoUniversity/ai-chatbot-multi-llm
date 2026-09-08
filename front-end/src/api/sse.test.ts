// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { consumeChatStream, readSse } from './sse'
import type { ChatEvent } from './types'

const meta = 'event: meta\ndata: {"version":"1","requestId":"r1","model":"luna","remainingQuota":null}\n\n'
const done = 'event: done\ndata: {"status":"completed","messageId":null}\n\n'
const frame = (event: string, data: object) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
function bytes(text: string, chunkSize = 1) {
  const data = new TextEncoder().encode(text)
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (let offset = 0; offset < data.length; offset += chunkSize) controller.enqueue(data.slice(offset, offset + chunkSize))
      controller.close()
    },
  })
}
async function collect(text: string) {
  const events: ChatEvent[] = []
  await consumeChatStream(bytes(text), new AbortController().signal, (event) => events.push(event))
  return events
}

describe('SSE framing and stream lifecycle', () => {
  it('preserves UTF-8 split at every byte, comments, and split CRLF delimiters', async () => {
    const text = '\u00e9 \u4e16\u754c \ud83d\ude80'
    const events: ChatEvent[] = []
    const stream = (`: keepalive\n\n${meta}${frame('delta', { text })}${done}`).replaceAll('\n', '\r\n')
    await consumeChatStream(bytes(stream), new AbortController().signal, (event) => events.push(event))
    expect(events.map((event) => event.type)).toEqual(['meta', 'delta', 'done'])
    expect(events[1]).toEqual({ type: 'delta', data: { text } })
  })

  it('handles multiple events in one chunk, CR-only lines and multiline data', async () => {
    const frames = []
    for await (const item of readSse(bytes(': comment\revent: delta\rdata: {"text":\rdata: "hello"}\r\r', 200), new AbortController().signal)) {
      frames.push(item)
    }
    expect(frames).toEqual([{ event: 'delta', data: '{"text":\n"hello"}' }])
  })

  it('ignores unknown additive event names but requires a real done event', async () => {
    const events = await collect(`${meta}event: extension\ndata: not-json\n\n${done}`)
    expect(events).toHaveLength(2)
    await expect(collect(`${meta}event: extension\ndata: {}\n\n`)).rejects.toMatchObject({ code: 'stream_interrupted' })
  })

  it('does not turn EOF or an unterminated done frame into completion', async () => {
    await expect(collect(meta + frame('delta', { text: 'Partial' }))).rejects.toMatchObject({ code: 'stream_interrupted' })
    await expect(collect(meta + done.trimEnd())).rejects.toMatchObject({ code: 'stream_interrupted' })
  })

  it('rejects malformed payloads, missing metadata, and version mismatch', async () => {
    await expect(collect(`${meta}event: delta\ndata: not-json\n\n`)).rejects.toMatchObject({ code: 'invalid_stream' })
    await expect(collect(done)).rejects.toMatchObject({ code: 'invalid_stream' })
    await expect(collect(meta.replace('"1"', '"2"') + done)).rejects.toMatchObject({ code: 'version_mismatch' })
    await expect(collect(meta + meta + done)).rejects.toMatchObject({ code: 'invalid_stream' })
  })

  it('never reports successful done after an in-band error', async () => {
    const error = frame('error', { code: 'upstream_error', message: 'Unavailable', requestId: 'r1' })
    await expect(collect(meta + error + done)).rejects.toMatchObject({ code: 'invalid_stream' })
    const events = await collect(meta + error + done.replace('completed', 'failed'))
    expect(events.at(-1)).toEqual({ type: 'done', data: { status: 'failed', messageId: null } })
    await expect(collect(meta + error + frame('delta', { text: 'invalid' }))).rejects.toThrow('continued after')
  })

  it('keeps unavailable usage null rather than inventing zero', async () => {
    const events = await collect(meta + frame('usage', { promptTokens: null, completionTokens: null, source: 'unavailable' }) + done)
    expect(events[1]).toEqual({ type: 'usage', data: { promptTokens: null, completionTokens: null, source: 'unavailable' } })
    expect((await collect(meta + done)).some((event) => event.type === 'usage')).toBe(false)
  })

  it('cancels the underlying reader when aborted and releases its lock', async () => {
    let cancelled = false
    const controller = new AbortController()
    const stream = new ReadableStream<Uint8Array>({ cancel() { cancelled = true } })
    const result = consumeChatStream(stream, controller.signal, () => {})
    controller.abort()
    await expect(result).rejects.toMatchObject({ name: 'AbortError' })
    expect(cancelled).toBe(true)
    expect(stream.locked).toBe(false)
  })

  it('cancels after terminal done without waiting indefinitely for EOF', async () => {
    let cancelled = false
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new TextEncoder().encode(meta + done)) },
      cancel() { cancelled = true },
    })
    const events: ChatEvent[] = []
    await consumeChatStream(stream, new AbortController().signal, (event) => events.push(event))
    expect(cancelled).toBe(true)
    expect(events.at(-1)?.type).toBe('done')
  })
})
