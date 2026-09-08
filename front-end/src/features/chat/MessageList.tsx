import { useEffect, useRef } from 'react'
import { ProblemNotice } from '../../components/ProblemNotice'
import type { DisplayMessage, Workspace } from './useWorkspace'

function UsageSummary({ message }: { message: DisplayMessage }) {
  const { usage } = message
  if (!usage || usage.source !== 'provider') return <span>Usage unavailable</span>
  return <span>Provider usage: {usage.promptTokens ?? 'unknown'} input / {usage.completionTokens ?? 'unknown'} output tokens</span>
}

const statuses = {
  pending: 'Generating',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Stopped locally',
  interrupted: 'Interrupted',
}

export function MessageList({ workspace }: { workspace: Workspace }) {
  const region = useRef<HTMLDivElement>(null)
  const nearBottom = useRef(true)
  useEffect(() => {
    const element = region.current
    if (element && nearBottom.current) element.scrollTop = element.scrollHeight
  }, [workspace.messages])
  useEffect(() => { nearBottom.current = true }, [workspace.active?.id])
  return (
    <div ref={region} className="message-scroll" onScroll={() => {
      const element = region.current
      if (element) nearBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 100
    }}>
      <div className="message-thread" aria-label="Chat messages">
        {workspace.messages.map((message) => <article key={message.localKey ?? message.id}
          className={`message message-${message.role}`} aria-label={message.role === 'user' ? 'Your message' : `${message.model ?? 'Model'} response`}>
          <div className={`message-avatar ${message.role === 'assistant' ? 'model-avatar' : ''}`} aria-hidden="true">{message.role === 'user' ? 'Y' : 'M'}</div>
          <div className="message-main">
            <div className="message-heading"><strong>{message.role === 'user' ? 'You' : message.model || 'Assistant'}</strong>
              {message.role === 'assistant' && <span className={`message-status status-${message.status}`}>
                {message.status === 'pending' && message.localKey && workspace.generating && <span className="pulse-dot" />}
                {message.status === 'pending' && !message.localKey ? 'Unfinalized' : statuses[message.status]}
              </span>}
            </div>
            <div className="message-content">{message.content || (message.status === 'pending' ? 'Waiting for a response...' : 'No response text was received.')}</div>
            {message.role === 'assistant' && <>
              {message.problem && <ProblemNotice problem={message.problem} />}
              {message.status === 'cancelled' && <p className="message-explanation">Stop requested. Provider cancellation is best-effort; final usage and billing may still change.{workspace.discovery?.historyEnabled && ' Refresh history to check the saved result.'}</p>}
              {message.status === 'pending' && !workspace.generating && <p className="message-explanation">This saved request has not been finalized. Refresh history to check its state; it will not be restarted automatically.</p>}
              <div className="message-metadata">
                <UsageSummary message={message} />
                <span>{message.remainingQuota === undefined || message.remainingQuota === null ? 'Quota snapshot unavailable' : `Approx. remaining quota: ${message.remainingQuota.toLocaleString()} tokens`}</span>
              </div>
              {message.remainingQuota !== undefined && message.remainingQuota !== null && <p className="quota-note">Gateway snapshot, not an exact balance or billing statement.</p>}
              {message.requestId && !message.problem?.requestId && <p className="request-id">Request ID: {message.requestId}</p>}
              {workspace.discovery?.historyEnabled && message.persisted && message.status !== 'pending' && <div className="feedback" aria-label="Response feedback">
                <span>Was this helpful?</span>
                <button className="feedback-button" aria-label="Helpful response" aria-pressed={message.feedback === 'up'}
                  disabled={Boolean(workspace.feedbackBusy)} onClick={() => { void workspace.rate(message, message.feedback === 'up' ? null : 'up') }}>Yes</button>
                <button className="feedback-button" aria-label="Unhelpful response" aria-pressed={message.feedback === 'down'}
                  disabled={Boolean(workspace.feedbackBusy)} onClick={() => { void workspace.rate(message, message.feedback === 'down' ? null : 'down') }}>No</button>
                {workspace.feedbackBusy === message.id && <span role="status">Saving feedback...</span>}
              </div>}
            </>}
          </div>
        </article>)}
        {workspace.viewBusy && <p className="history-loading" role="status">Loading saved messages...</p>}
        {workspace.messageToken && <div className="history-pagination"><p>More saved messages are available. Load them before continuing this conversation.</p>
          <button className="secondary-button" disabled={workspace.viewBusy} onClick={() => { void workspace.moreMessages() }}>Load more messages</button>
        </div>}
      </div>
    </div>
  )
}
