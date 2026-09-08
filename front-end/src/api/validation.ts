import { ApiError, StreamError } from './errors'
import type { ChatEvent, Conversation, Message, ModelDiscovery, Page, Usage } from './types'

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const isCount = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0
const nullableCount = (value: unknown): value is number | null => value === null || isCount(value)

export function readUsage(value: unknown): Usage | undefined {
  if (!isRecord(value) || !nullableCount(value.promptTokens) || !nullableCount(value.completionTokens)) {
    return undefined
  }
  if (value.source !== 'provider' && value.source !== 'unavailable') return undefined
  return {
    promptTokens: value.source === 'provider' ? value.promptTokens : null,
    completionTokens: value.source === 'provider' ? value.completionTokens : null,
    source: value.source,
  }
}

function invalidResponse(): never {
  throw new ApiError({ code: 'invalid_response', message: 'The API returned an unexpected response. Refresh or contact the application administrator.' })
}

export function readModels(value: unknown): ModelDiscovery {
  if (!isRecord(value) || !Array.isArray(value.models) || typeof value.historyEnabled !== 'boolean') {
    return invalidResponse()
  }
  const models = value.models.map((model: unknown) => {
    if (!isRecord(model) || typeof model.id !== 'string' || !model.id) return invalidResponse()
    return { id: model.id }
  })
  return { models, historyEnabled: value.historyEnabled }
}

export function readConversation(value: unknown): Conversation {
  if (!isRecord(value) || typeof value.id !== 'string' || typeof value.title !== 'string'
    || typeof value.createdAt !== 'string' || typeof value.updatedAt !== 'string') return invalidResponse()
  return { id: value.id, title: value.title, createdAt: value.createdAt, updatedAt: value.updatedAt }
}

export function readMessage(value: unknown): Message {
  if (!isRecord(value) || typeof value.id !== 'string' || typeof value.conversationId !== 'string'
    || typeof value.content !== 'string' || typeof value.createdAt !== 'string'
    || (value.role !== 'user' && value.role !== 'assistant')
    || (value.status !== 'pending' && value.status !== 'completed' && value.status !== 'failed'
      && value.status !== 'cancelled' && value.status !== 'interrupted')) return invalidResponse()
  return {
    id: value.id, conversationId: value.conversationId, role: value.role,
    content: value.content, status: value.status, createdAt: value.createdAt,
    model: typeof value.model === 'string' ? value.model : undefined,
    usage: readUsage(value.usage),
    feedback: value.feedback === 'up' || value.feedback === 'down' ? value.feedback : null,
  }
}

export function readPage<T>(value: unknown, itemReader: (item: unknown) => T): Page<T> {
  if (!isRecord(value) || !Array.isArray(value.items)
    || !(value.continuationToken === null || typeof value.continuationToken === 'string')) return invalidResponse()
  return { items: value.items.map(itemReader), continuationToken: value.continuationToken }
}

export function readChatEvent(name: string, json: string): ChatEvent | undefined {
  // Unknown event names are additive extensions to v1, not completion signals.
  if (!['meta', 'delta', 'usage', 'error', 'done'].includes(name)) return undefined
  let data: unknown
  try {
    data = JSON.parse(json)
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error
    throw new StreamError('The response contained malformed event data.')
  }
  if (!isRecord(data)) throw new StreamError('The response contained an invalid event.')
  switch (name) {
    case 'meta':
      if (data.version !== '1') throw new StreamError('This API stream version is not supported. Update the application.', 'version_mismatch')
      if (typeof data.requestId === 'string' && typeof data.model === 'string' && nullableCount(data.remainingQuota)
        && (data.conversationId === undefined || typeof data.conversationId === 'string')
        && (data.messageId === undefined || typeof data.messageId === 'string')) {
        return { type: 'meta', data: {
          version: '1', requestId: data.requestId, model: data.model,
          remainingQuota: data.remainingQuota,
          conversationId: data.conversationId, messageId: data.messageId,
        } }
      }
      break
    case 'delta':
      if (typeof data.text === 'string') return { type: 'delta', data: { text: data.text } }
      break
    case 'usage': {
      const usage = readUsage(data)
      if (usage) return { type: 'usage', data: usage }
      break
    }
    case 'error':
      if (typeof data.code === 'string' && typeof data.message === 'string' && typeof data.requestId === 'string'
        && (data.retryAfter === undefined || isCount(data.retryAfter))) {
        return { type: 'error', data: {
          code: data.code, message: data.message, requestId: data.requestId,
          retryAfter: data.retryAfter,
        } }
      }
      break
    case 'done':
      if ((data.status === 'completed' || data.status === 'failed') && (data.messageId === null || typeof data.messageId === 'string')) {
        return { type: 'done', data: { status: data.status, messageId: data.messageId } }
      }
  }
  throw new StreamError(`The response contained an invalid ${name} event.`)
}
