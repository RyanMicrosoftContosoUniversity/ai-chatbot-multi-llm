export type Rating = 'up' | 'down' | null
export type MessageStatus = 'pending' | 'completed' | 'failed' | 'cancelled' | 'interrupted'

export interface Usage {
  promptTokens: number | null
  completionTokens: number | null
  source: 'provider' | 'unavailable'
}

export interface ApiProblem {
  code: string
  message: string
  requestId?: string
  retryAfter?: number
}

export interface Conversation {
  id: string
  title: string
  createdAt: string
  updatedAt: string
}

export interface Message {
  id: string
  conversationId: string
  role: 'user' | 'assistant'
  content: string
  status: MessageStatus
  createdAt: string
  model?: string
  usage?: Usage
  feedback?: Rating
}

export interface Page<T> {
  items: T[]
  continuationToken: string | null
}

export interface ModelDiscovery {
  models: { id: string }[]
  historyEnabled: boolean
}

export interface ChatInput {
  model: string
  message: string
  maxTokens?: number
}

export type ChatEvent =
  | { type: 'meta'; data: {
    version: '1'
    requestId: string
    model: string
    remainingQuota: number | null
    conversationId?: string
    messageId?: string
  } }
  | { type: 'delta'; data: { text: string } }
  | { type: 'usage'; data: Usage }
  | { type: 'error'; data: ApiProblem }
  | { type: 'done'; data: { status: 'completed' | 'failed'; messageId: string | null } }

export interface ChatApi {
  models(signal: AbortSignal): Promise<ModelDiscovery>
  conversations(signal: AbortSignal, continuationToken?: string): Promise<Page<Conversation>>
  createConversation(title: string, signal: AbortSignal): Promise<Conversation>
  renameConversation(id: string, title: string, signal: AbortSignal): Promise<Conversation>
  deleteConversation(id: string, signal: AbortSignal): Promise<void>
  messages(id: string, signal: AbortSignal, continuationToken?: string): Promise<Page<Message>>
  feedback(conversationId: string, messageId: string, rating: Rating, signal: AbortSignal): Promise<void>
  stream(
    input: ChatInput,
    signal: AbortSignal,
    onEvent: (event: ChatEvent) => void,
    conversation?: { id: string; clientMessageId: string },
  ): Promise<void>
}
