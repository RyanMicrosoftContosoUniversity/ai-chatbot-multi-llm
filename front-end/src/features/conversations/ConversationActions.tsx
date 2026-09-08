import { useEffect, useRef, useState } from 'react'
import type { Workspace } from '../chat/useWorkspace'

export function ConversationActions({ workspace }: { workspace: Workspace }) {
  const [action, setAction] = useState<'rename' | 'delete' | null>(null)
  const [title, setTitle] = useState('')
  const dialog = useRef<HTMLDialogElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const returnFocus = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    if (action) {
      dialog.current?.showModal()
      if (action === 'rename') input.current?.focus()
    } else {
      dialog.current?.close()
      returnFocus.current?.focus()
    }
  }, [action])
  if (!workspace.active) return null
  const disabled = workspace.generating || workspace.mutationBusy || workspace.viewBusy
  return <>
    <div className="conversation-actions">
      <button className="text-button" disabled={disabled} onClick={(event) => {
        returnFocus.current = event.currentTarget
        setTitle(workspace.active?.title ?? '')
        setAction('rename')
      }}>Rename</button>
      <button className="text-button" disabled={disabled} onClick={(event) => {
        returnFocus.current = event.currentTarget
        setAction('delete')
      }}>Delete</button>
      <button className="text-button" disabled={disabled} onClick={() => { if (workspace.active) void workspace.open(workspace.active) }}>Refresh history</button>
    </div>
    <dialog ref={dialog} className="conversation-dialog" aria-labelledby="conversation-action-title"
      onCancel={(event) => { if (workspace.mutationBusy) event.preventDefault(); else setAction(null) }}>
      <form onSubmit={async (event) => {
        event.preventDefault()
        if (action && await workspace.editConversation(action, title)) setAction(null)
      }}>
        <p className="eyebrow">YOUR WORKSPACE</p>
        <h2 id="conversation-action-title">{action === 'delete' ? 'Delete this conversation?' : 'Name this conversation'}</h2>
        {action === 'delete' ? <p>This removes the saved conversation and its messages. This cannot be undone.</p>
          : <label>Conversation name<input ref={input} value={title} maxLength={120} required onChange={(event) => setTitle(event.target.value)} /></label>}
        {workspace.problem && <p role="alert">{workspace.problem.message}</p>}
        <div className="dialog-actions">
          <button type="button" className="secondary-button" disabled={workspace.mutationBusy} onClick={() => setAction(null)}>Cancel</button>
          <button className={action === 'delete' ? 'danger-button' : 'primary-button'}
            disabled={workspace.mutationBusy || (action === 'rename' && !title.trim())}>
            {workspace.mutationBusy ? 'Saving...' : action === 'delete' ? 'Delete conversation' : 'Save name'}
          </button>
        </div>
      </form>
    </dialog>
  </>
}
