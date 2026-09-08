import { useEffect, useRef, useState } from 'react'
import type { Workspace } from './useWorkspace'

export function Composer({ workspace, suggestion }: { workspace: Workspace; suggestion: { text: string; key: number } | null }) {
  const [text, setText] = useState(suggestion?.text ?? '')
  const input = useRef<HTMLTextAreaElement>(null)
  const submitting = useRef(false)
  const wasGenerating = useRef(false)
  useEffect(() => {
    if (suggestion) input.current?.focus()
  }, [suggestion])
  useEffect(() => {
    if (wasGenerating.current && !workspace.generating) input.current?.focus()
    wasGenerating.current = workspace.generating
  }, [workspace.generating])
  const disabled = workspace.loading || workspace.viewBusy || !workspace.historyReady || workspace.mutationBusy
    || !workspace.model || Boolean(workspace.messageToken)
  async function submit() {
    if (submitting.current || disabled || workspace.generating || !text.trim()) return
    submitting.current = true
    const original = text
    setText('')
    const accepted = await workspace.send(original)
    if (!accepted) setText(original)
    submitting.current = false
  }
  return <div className="composer-area">
    <form className={`composer ${workspace.generating ? 'composer-active' : ''}`} onSubmit={(event) => { event.preventDefault(); void submit() }}>
      <label className="visually-hidden" htmlFor="chat-message">Message</label>
      <textarea id="chat-message" ref={input} value={text} rows={3}
        placeholder={workspace.model ? `Message ${workspace.model}...` : 'Connect to an available model to begin...'}
        disabled={disabled || workspace.generating}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault()
            void submit()
          }
        }}
        aria-describedby="composer-help"
      />
      <div className="composer-bottom">
        <span className="composer-mode"><span className="small-diamond" aria-hidden="true" />{workspace.discovery?.historyEnabled ? 'Server-owned conversation context' : 'Fresh context with every message'}</span>
        {workspace.generating
          ? <button className="stop-button" type="button" onClick={workspace.stop}><span aria-hidden="true">&#9632;</span> Stop</button>
          : <button className="send-button" type="submit" disabled={disabled || !text.trim()} aria-label="Send message"><span>Send</span><span aria-hidden="true">&#8593;</span></button>}
      </div>
    </form>
    <div id="composer-help" className="composer-help"><span>AI can make mistakes. Review important information.</span><span>Enter to send <span aria-hidden="true">&middot;</span> Shift + Enter for a new line</span></div>
  </div>
}
