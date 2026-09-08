import { StreamError } from './errors'
import { readChatEvent } from './validation'
import type { ChatEvent } from './types'

export interface SseFrame { event: string; data: string }

export async function* readSse(
  stream: ReadableStream<Uint8Array>,
  signal: AbortSignal,
): AsyncGenerator<SseFrame> {
  const reader = stream.getReader()
  const decoder = new TextDecoder('utf-8', { fatal: true })
  let buffer = ''
  let event = ''
  let data: string[] = []
  let frameSize = 0
  let skipLf = false
  let finished = false
  const abort = () => { void reader.cancel(signal.reason) }
  signal.addEventListener('abort', abort, { once: true })
  try {
    signal.throwIfAborted()
    while (true) {
      const chunk = await reader.read()
      signal.throwIfAborted()
      finished = chunk.done
      buffer += decoder.decode(chunk.value, { stream: !chunk.done })
      let start = 0
      for (let index = 0; index < buffer.length; index++) {
        const char = buffer[index]
        if (skipLf) {
          skipLf = false
          if (char === '\n') {
            start = index + 1
            continue
          }
        }
        if (char !== '\r' && char !== '\n') continue
        const line = buffer.slice(start, index)
        start = index + 1
        skipLf = char === '\r'
        frameSize += line.length
        if (frameSize > 1_048_576) throw new StreamError('A response event exceeded the supported size.')
        if (line === '') {
          if (data.length) yield { event: event || 'message', data: data.join('\n') }
          event = ''
          data = []
          frameSize = 0
        } else if (!line.startsWith(':')) {
          const colon = line.indexOf(':')
          const field = colon < 0 ? line : line.slice(0, colon)
          let value = colon < 0 ? '' : line.slice(colon + 1)
          if (value.startsWith(' ')) value = value.slice(1)
          if (field === 'event') event = value
          if (field === 'data') data.push(value)
        }
      }
      buffer = buffer.slice(start)
      if (buffer.length + frameSize > 1_048_576) throw new StreamError('A response event exceeded the supported size.')
      if (chunk.done) break
    }
    // An unterminated frame at EOF is deliberately not dispatched.
  } finally {
    signal.removeEventListener('abort', abort)
    try {
      if (!finished) await reader.cancel()
    } finally {
      reader.releaseLock()
    }
  }
}

export async function consumeChatStream(
  stream: ReadableStream<Uint8Array>,
  signal: AbortSignal,
  onEvent: (event: ChatEvent) => void,
): Promise<void> {
  let metaSeen = false
  let errorSeen = false
  for await (const frame of readSse(stream, signal)) {
    signal.throwIfAborted()
    const event = readChatEvent(frame.event, frame.data)
    if (!event) continue
    if (event.type === 'meta') {
      if (metaSeen) throw new StreamError('The response repeated its stream metadata.')
      metaSeen = true
    } else if (!metaSeen) {
      throw new StreamError('The response did not begin with stream metadata.')
    }
    if (errorSeen && (event.type === 'delta' || event.type === 'error')) {
      throw new StreamError('The response continued after a generation failure.')
    }
    if (event.type === 'error') errorSeen = true
    if (event.type === 'done' && event.data.status === 'completed' && errorSeen) {
      throw new StreamError('The response reported success after a generation failure.')
    }
    onEvent(event)
    if (event.type === 'done') return
  }
  throw new StreamError('The connection ended without a completion event. The response may be incomplete; it was not retried.', 'stream_interrupted')
}
