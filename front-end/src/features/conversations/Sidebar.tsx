import type { AuthSession } from '../../auth/auth'
import type { Workspace } from '../chat/useWorkspace'
import { Brand } from '../../components/Brand'

export function Sidebar({ workspace, auth, expanded, onClose }: {
  workspace: Workspace
  auth: AuthSession
  expanded: boolean
  onClose(): void
}) {
  const { active, conversations, discovery } = workspace
  return (
    <aside id="workspace-sidebar" className={`sidebar ${expanded ? 'sidebar-open' : ''}`} aria-label="Workspace navigation">
      <div className="sidebar-brand"><Brand /><button className="icon-button mobile-only" onClick={onClose} aria-label="Close navigation">&times;</button></div>
      <button className="new-chat-button" disabled={workspace.loading || workspace.mutationBusy} onClick={() => { workspace.newChat(); onClose() }}>
        <span aria-hidden="true">+</span> New {discovery?.historyEnabled ? 'conversation' : 'chat'}
      </button>
      <div className="sidebar-section-label"><span>{discovery?.historyEnabled ? 'YOUR CONVERSATIONS' : 'YOUR WORKSPACE'}</span>
        {discovery?.historyEnabled ? <button className="refresh-list-button" aria-label="Refresh conversations" title="Refresh conversations"
          disabled={workspace.listBusy || workspace.loading} onClick={() => { void workspace.refreshConversations() }}>&#8635;</button> : <span>01</span>}
      </div>
      <nav className="conversation-list" aria-label="Conversations">
        {workspace.loading ? <p className="sidebar-note" role="status">Connecting to your workspace...</p>
          : discovery?.historyEnabled ? <>
            {!conversations.length && <p className="sidebar-note">A little space for your next idea. Your saved conversations will appear here.</p>}
            {conversations.map((item) => <button key={item.id}
              className={`conversation-link ${active?.id === item.id ? 'is-active' : ''}`}
              aria-current={active?.id === item.id ? 'page' : undefined}
              disabled={workspace.mutationBusy}
              onClick={() => { void workspace.open(item); onClose() }}>
              <span className="conversation-symbol" aria-hidden="true">&#9707;</span>
              <span>{item.title}</span>
            </button>)}
            {workspace.listToken && <button className="text-button load-conversations" disabled={workspace.listBusy}
              onClick={() => { void workspace.moreConversations() }}>{workspace.listBusy ? 'Loading...' : 'Load more conversations'}</button>}
          </> : <div className="sidebar-note">
            <p>One prompt. A fresh perspective.</p>
            <p>Each request is independent. Chat text is only kept in this page and is cleared when you leave or reload.</p>
          </div>}
      </nav>
      <div className="workspace-note"><span className="status-dot" /><div><strong>{discovery?.historyEnabled ? 'History enabled' : discovery ? 'Single-turn mode' : 'Awaiting discovery'}</strong>
        <p>{discovery?.historyEnabled ? 'Saved to your tenant-scoped account.' : 'No conversation memory is sent.'}</p></div></div>
      <div className="account-panel">
        <span className="avatar" aria-hidden="true">{auth.account?.name.slice(0, 1).toUpperCase()}</span>
        <div className="account-details"><strong>{auth.account?.name}</strong><span title={auth.account?.username}>{auth.account?.username}</span></div>
        <button className="icon-button" disabled={auth.busy} onClick={() => { void auth.signOut() }} aria-label="Sign out" title="Sign out">&#8599;</button>
      </div>
      {auth.accounts.length > 1 && <label className="account-select">Switch account
        <select value={auth.account?.id ?? ''} disabled={auth.busy} onChange={(event) => auth.selectAccount(event.target.value)}>
          {auth.accounts.map((item) => <option key={item.id} value={item.id}>{item.username || item.name}</option>)}
        </select>
      </label>}
    </aside>
  )
}
