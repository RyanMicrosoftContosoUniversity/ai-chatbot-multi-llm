import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, isAbort, problemFrom } from '../../api/errors'
import type { ApiProblem, ChatApi, ChatEvent, Conversation, Message, ModelDiscovery, Rating } from '../../api/types'

export interface DisplayMessage extends Message {
  localKey?: string
  persisted?: boolean
  problem?: ApiProblem
  requestId?: string
  remainingQuota?: number | null
}

function mergeMessages(existing: DisplayMessage[], incoming: Message[]): DisplayMessage[] {
  const combined = new Map(existing.map((item) => [item.id, item]))
  incoming.forEach((item) => combined.set(item.id, { ...item, persisted: true }))
  return [...combined.values()].sort((a, b) => a.createdAt.localeCompare(b.createdAt))
}

export function useWorkspace(api: ChatApi) {
  const [discovery, setDiscovery] = useState<ModelDiscovery | null>(null)
  const [loading, setLoading] = useState(true)
  const [model, setModel] = useState('')
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [listToken, setListToken] = useState<string | null>(null)
  const [listBusy, setListBusy] = useState(false)
  const [active, setActive] = useState<Conversation | null>(null)
  const [messages, setMessages] = useState<DisplayMessage[]>([])
  const [messageToken, setMessageToken] = useState<string | null>(null)
  const [viewBusy, setViewBusy] = useState(false)
  const [historyReady, setHistoryReady] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [mutationBusy, setMutationBusy] = useState(false)
  const [problem, setProblem] = useState<ApiProblem | null>(null)
  const [feedbackBusy, setFeedbackBusy] = useState<string | null>(null)
  const [reload, setReload] = useState(0)
  const requests = useRef(new Set<AbortController>())
  const generation = useRef<AbortController | null>(null)
  const navigation = useRef<AbortController | null>(null)
  const mutation = useRef(false)
  const mounted = useRef(false)

  const begin = useCallback(() => {
    const controller = new AbortController()
    requests.current.add(controller)
    return controller
  }, [])
  const finish = useCallback((controller: AbortController) => { requests.current.delete(controller) }, [])
  const report = useCallback((error: unknown, signal: AbortSignal) => {
    if (!signal.aborted && !isAbort(error) && mounted.current) setProblem(problemFrom(error))
  }, [])

  useEffect(() => {
    mounted.current = true
    const activeRequests = requests.current
    const controller = begin()
    const { signal } = controller
    async function load() {
      try {
        const result = await api.models(signal)
        if (signal.aborted) return
        setDiscovery(result)
        setModel((current) => result.models.some((item) => item.id === current) ? current : result.models[0]?.id ?? '')
        if (result.historyEnabled) {
          setListBusy(true)
          const page = await api.conversations(signal)
          if (signal.aborted) return
          setConversations(page.items)
          setListToken(page.continuationToken)
        }
      } catch (error) {
        report(error, signal)
      } finally {
        finish(controller)
        if (!signal.aborted) {
          setLoading(false)
          setListBusy(false)
        }
      }
    }
    void load()
    return () => {
      mounted.current = false
      activeRequests.forEach((request) => request.abort())
      activeRequests.clear()
    }
  }, [api, begin, finish, report, reload])

  const stop = useCallback(() => { generation.current?.abort() }, [])

  async function open(conversation: Conversation) {
    stop()
    navigation.current?.abort()
    const controller = begin()
    navigation.current = controller
    setActive(conversation)
    setMessages([])
    setMessageToken(null)
    setViewBusy(true)
    setHistoryReady(false)
    setProblem(null)
    try {
      const page = await api.messages(conversation.id, controller.signal)
      if (!controller.signal.aborted) {
        setMessages(mergeMessages([], page.items))
        setMessageToken(page.continuationToken)
        setHistoryReady(true)
      }
    } catch (error) {
      report(error, controller.signal)
      if (!controller.signal.aborted) setMessageToken(null)
    } finally {
      finish(controller)
      if (!controller.signal.aborted) setViewBusy(false)
    }
  }

  function newChat() {
    stop()
    navigation.current?.abort()
    setActive(null)
    setMessages([])
    setMessageToken(null)
    setViewBusy(false)
    setHistoryReady(true)
    setProblem(null)
  }

  async function loadConversations(reset: boolean) {
    if (listBusy || (!reset && !listToken)) return
    const controller = begin()
    setListBusy(true)
    try {
      const page = await api.conversations(controller.signal, reset ? undefined : listToken ?? undefined)
      if (controller.signal.aborted) return
      setConversations((previous) => reset ? page.items : [...new Map([...previous, ...page.items].map((item) => [item.id, item])).values()])
      setListToken(page.continuationToken)
    } catch (error) {
      report(error, controller.signal)
    } finally {
      finish(controller)
      if (!controller.signal.aborted) setListBusy(false)
    }
  }

  async function moreMessages() {
    if (!active || viewBusy || !messageToken) return
    const controller = begin()
    navigation.current = controller
    setViewBusy(true)
    try {
      const page = await api.messages(active.id, controller.signal, messageToken)
      if (controller.signal.aborted) return
      setMessages((previous) => mergeMessages(previous, page.items))
      setMessageToken(page.continuationToken)
    } catch (error) {
      report(error, controller.signal)
    } finally {
      finish(controller)
      if (!controller.signal.aborted) setViewBusy(false)
    }
  }

  async function send(text: string): Promise<boolean> {
    if (generation.current || viewBusy || !historyReady || mutation.current || !model || !text.trim() || !discovery || messageToken) return false
    const controller = begin()
    generation.current = controller
    setGenerating(true)
    setProblem(null)
    const clientMessageId = crypto.randomUUID()
    const localKey = crypto.randomUUID()
    let target = active
    let appended = false
    const update = (change: Partial<DisplayMessage> | ((item: DisplayMessage) => Partial<DisplayMessage>)) => {
      if (!mounted.current) return
      setMessages((items) => items.map((item) => item.localKey === localKey
        ? { ...item, ...(typeof change === 'function' ? change(item) : change) } : item))
    }
    try {
      if (discovery.historyEnabled && !target) {
        const created = await api.createConversation(text.trim().slice(0, 72), controller.signal)
        target = created
        controller.signal.throwIfAborted()
        setActive(target)
        setConversations((items) => [created, ...items])
      }
      controller.signal.throwIfAborted()
      const createdAt = new Date().toISOString()
      const user: DisplayMessage = {
        id: clientMessageId, conversationId: target?.id ?? '', role: 'user',
        content: text, status: 'completed', createdAt,
      }
      const assistant: DisplayMessage = {
        id: localKey, localKey, conversationId: target?.id ?? '', role: 'assistant',
        content: '', status: 'pending', model, createdAt,
      }
      // Earlier single-turn results remain visible but are never sent as context.
      setMessages((items) => [...items, user, assistant])
      appended = true
      let terminalSeen = false
      let inBandError: ApiProblem | undefined
      const onEvent = (event: ChatEvent) => {
        if (controller.signal.aborted || !mounted.current) return
        switch (event.type) {
          case 'meta':
            update({
              requestId: event.data.requestId, remainingQuota: event.data.remainingQuota,
              model: event.data.model, ...(event.data.messageId ? { id: event.data.messageId } : {}),
            })
            break
          case 'delta':
            update((item) => ({ content: item.content + event.data.text }))
            break
          case 'usage':
            update({ usage: event.data })
            break
          case 'error':
            inBandError = event.data
            update({ problem: event.data })
            break
          case 'done':
            terminalSeen = true
            update({
              status: event.data.status,
              persisted: Boolean(target && event.data.messageId),
              ...(event.data.messageId ? { id: event.data.messageId } : {}),
              ...(event.data.status === 'failed' && !inBandError
                ? { problem: { code: 'generation_failed', message: 'The server could not complete this response. No automatic retry was made.' } } : {}),
            })
        }
      }
      await api.stream({ model, message: text }, controller.signal, onEvent,
        target ? { id: target.id, clientMessageId } : undefined)
      controller.signal.throwIfAborted()
      if (!terminalSeen) {
        throw new ApiError({ code: 'stream_interrupted', message: 'The response ended without a completion event. It may be incomplete.' })
      }
    } catch (error) {
      if (controller.signal.aborted || isAbort(error)) {
        update({ status: 'cancelled', problem: undefined })
      } else if (appended) {
        const failure = problemFrom(error)
        update((item) => ({
          status: failure.code === 'stream_interrupted' || failure.code === 'invalid_stream'
            || failure.code === 'version_mismatch' || !(error instanceof ApiError) ? 'interrupted' : 'failed',
          problem: item.problem ?? failure,
        }))
      } else {
        report(error, controller.signal)
      }
    } finally {
      finish(controller)
      if (generation.current === controller) {
        generation.current = null
        if (mounted.current) setGenerating(false)
      }
    }
    return appended
  }

  async function editConversation(action: 'rename' | 'delete', title?: string): Promise<boolean> {
    if (!active || generation.current || mutation.current) return false
    mutation.current = true
    setMutationBusy(true)
    setProblem(null)
    const controller = begin()
    try {
      if (action === 'delete') {
        await api.deleteConversation(active.id, controller.signal)
        if (controller.signal.aborted) return false
        setConversations((items) => items.filter((item) => item.id !== active.id))
        newChat()
      } else {
        if (!title?.trim()) return false
        const updated = await api.renameConversation(active.id, title.trim(), controller.signal)
        if (controller.signal.aborted) return false
        setActive(updated)
        setConversations((items) => items.map((item) => item.id === updated.id ? updated : item))
      }
      return true
    } catch (error) {
      report(error, controller.signal)
      return false
    } finally {
      finish(controller)
      mutation.current = false
      if (mounted.current) setMutationBusy(false)
    }
  }

  async function rate(message: DisplayMessage, rating: Rating) {
    if (!active || !message.persisted || feedbackBusy) return
    const controller = begin()
    setFeedbackBusy(message.id)
    setProblem(null)
    try {
      await api.feedback(active.id, message.id, rating, controller.signal)
      if (!controller.signal.aborted) setMessages((items) => items.map((item) => item.id === message.id ? { ...item, feedback: rating } : item))
    } catch (error) {
      report(error, controller.signal)
    } finally {
      finish(controller)
      if (!controller.signal.aborted) setFeedbackBusy(null)
    }
  }

  return {
    discovery, loading, model, setModel, conversations, listToken, listBusy, active, messages, messageToken,
    viewBusy, historyReady, generating, mutationBusy, problem, feedbackBusy, stop, open, newChat, send,
    moreConversations: () => loadConversations(false),
    refreshConversations: () => loadConversations(true),
    moreMessages, editConversation, rate,
    retryDiscovery: () => { setLoading(true); setProblem(null); setReload((value) => value + 1) },
    dismissProblem: () => setProblem(null),
  }
}

export type Workspace = ReturnType<typeof useWorkspace>
