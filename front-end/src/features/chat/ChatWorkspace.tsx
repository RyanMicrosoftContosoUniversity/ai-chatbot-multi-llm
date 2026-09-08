import { useState } from 'react'
import type { ChatApi } from '../../api/types'
import type { AuthSession } from '../../auth/auth'
import { ProblemNotice } from '../../components/ProblemNotice'
import { Sidebar } from '../conversations/Sidebar'
import { ConversationActions } from '../conversations/ConversationActions'
import { Composer } from './Composer'
import { MessageList } from './MessageList'
import { useWorkspace } from './useWorkspace'

const prompts = [
  { number: '01', title: 'Find the clarity', caption: 'Turn complexity into a next step', text: 'Help me break down a complex decision. Start by asking me what I am deciding and what matters most.' },
  { number: '02', title: 'Explore an idea', caption: 'Look at a problem differently', text: 'Help me brainstorm a new idea. Ask me about the problem I want to solve, then suggest three different approaches.' },
  { number: '03', title: 'Build understanding', caption: 'Make something unfamiliar click', text: 'Help me understand a technical concept. Ask me what I want to learn and how familiar I am with the topic.' },
]

export function ChatWorkspace({ api, auth }: { api: ChatApi; auth: AuthSession }) {
  const workspace = useWorkspace(api)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [suggestion, setSuggestion] = useState<{ text: string; key: number } | null>(null)
  const [chatKey, setChatKey] = useState(0)
  const view = {
    ...workspace,
    newChat: () => { workspace.newChat(); setSuggestion(null); setChatKey((key) => key + 1) },
    open: async (...args: Parameters<typeof workspace.open>) => {
      setSuggestion(null)
      setChatKey((key) => key + 1)
      await workspace.open(...args)
    },
  }
  const empty = !workspace.messages.length && !workspace.viewBusy
  return <div className="workspace">
    <a className="skip-link" href="#chat-main">Skip to chat</a>
    {sidebarOpen && <button className="sidebar-overlay" aria-label="Close navigation" onClick={() => setSidebarOpen(false)} />}
    <Sidebar workspace={view} auth={auth} expanded={sidebarOpen} onClose={() => setSidebarOpen(false)} />
    <main id="chat-main" className="chat-main" tabIndex={-1}>
      <header className="chat-header">
        <div className="chat-header-heading">
          <button className="icon-button mobile-only" aria-label="Open navigation" aria-controls="workspace-sidebar" aria-expanded={sidebarOpen}
            onClick={() => setSidebarOpen(!sidebarOpen)}>&#9776;</button>
          <div><p className="eyebrow">A SPACE TO THINK</p><h1>{workspace.active?.title ?? 'New conversation'}</h1></div>
        </div>
        <label className="model-selector"><span>MODEL</span>
          <select aria-label="Model" value={workspace.model} onChange={(event) => workspace.setModel(event.target.value)}
            disabled={workspace.loading || workspace.generating || !workspace.discovery?.models.length}>
            {!workspace.discovery?.models.length && <option value="">{workspace.loading ? 'Discovering models...' : 'No available models'}</option>}
            {workspace.discovery?.models.map((model) => <option key={model.id} value={model.id}>{model.id}</option>)}
          </select>
        </label>
      </header>
      <div className="context-bar"><span><span className="status-dot" />{workspace.discovery?.historyEnabled ? 'Saved conversation' : 'Single-turn chat'}</span>
        <ConversationActions key={workspace.active?.id ?? 'new'} workspace={workspace} />
      </div>
      <div className="workspace-notices">
        {auth.error && <ProblemNotice problem={{ code: 'authentication_failed', message: auth.error }} />}
        {auth.needsInteraction && <div className="reconnect-notice" role="alert"><p>Your session needs attention. Reconnect, then retry your request explicitly.</p>
          <button className="secondary-button" disabled={auth.busy} onClick={() => { workspace.stop(); void auth.reconnect() }}>Reconnect</button></div>}
        {workspace.problem && <ProblemNotice problem={workspace.problem} onDismiss={workspace.dismissProblem} />}
        {!workspace.loading && (!workspace.discovery || !workspace.discovery.models.length) && <div className="discovery-notice">
          <p>{workspace.discovery ? 'No allowed model aliases are currently available. No substitute model will be selected.' : 'Model discovery is unavailable. Check your access and API configuration.'}</p>
          <button className="secondary-button" onClick={workspace.retryDiscovery}>Refresh models</button>
        </div>}
      </div>
      {empty ? <div className="empty-chat">
        <div className="orbit-art" aria-hidden="true"><div /><div /><span>M</span></div>
        <p className="eyebrow">DIFFERENT MODELS. NEW PERSPECTIVES.</p>
        <h2>Where will your<br /><span>curiosity take you?</span></h2>
        <p className="empty-description">{workspace.discovery?.historyEnabled
          ? 'A focused place to explore, create, and keep the conversation going.'
          : 'Choose a model and start with a question. Each message is a fresh, independent conversation.'}</p>
        <div className="prompt-grid">{prompts.map((prompt) => <button className="prompt-card" key={prompt.number}
          disabled={workspace.loading || workspace.generating || workspace.mutationBusy || !workspace.model} onClick={() => setSuggestion({ text: prompt.text, key: Date.now() })}>
          <span className="prompt-number">{prompt.number}<span aria-hidden="true">&#8599;</span></span>
          <strong>{prompt.title}</strong><span>{prompt.caption}</span>
        </button>)}</div>
      </div> : <MessageList workspace={workspace} />}
      <p className="visually-hidden" role="status" aria-live="polite">
        {workspace.loading ? 'Loading workspace' : workspace.generating ? 'Generating response' : workspace.messages.at(-1)?.role === 'assistant'
          ? `Response ${workspace.messages.at(-1)?.status}` : 'Ready'}
      </p>
      <Composer key={`${chatKey}-${suggestion?.key ?? 0}`} workspace={workspace} suggestion={suggestion} />
    </main>
  </div>
}
