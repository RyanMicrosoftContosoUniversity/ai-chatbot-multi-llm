import type { ChatApi } from './api/types'
import type { AuthSession } from './auth/auth'
import { Brand } from './components/Brand'
import { ChatWorkspace } from './features/chat/ChatWorkspace'

export function SetupState({ message, loading = false }: { message: string; loading?: boolean }) {
  return <main className="auth-page"><Brand /><div className="auth-card">
    <p className="eyebrow">YOUR AI WORKSPACE</p>
    <h1>{loading ? 'Opening your workspace' : 'A little setup is needed'}</h1>
    <p role={loading ? 'status' : 'alert'}>{message}</p>
    {!loading && <p>Check the public Vite configuration and Entra application settings, then rebuild. No unauthenticated fallback is available.</p>}
  </div></main>
}

export default function App({ auth, api }: { auth: AuthSession; api: ChatApi }) {
  if (auth.account) return <ChatWorkspace key={auth.account.id} auth={auth} api={api} />
  return <main className="auth-page">
    <Brand />
    <div className="auth-card">
      <div className="auth-decoration" aria-hidden="true"><span /><span /><span /></div>
      <p className="eyebrow">ONE WORKSPACE. MANY PERSPECTIVES.</p>
      <h1>Your next idea<br /><span>starts here.</span></h1>
      <p>Explore different AI models in one calm, focused workspace. Sign in with your organization account to get started.</p>
      {auth.error && <div className="problem-notice" role="alert">{auth.error}</div>}
      <button className="primary-button sign-in-button" disabled={auth.busy} onClick={() => { void auth.signIn() }}>
        <span className="microsoft-mark" aria-hidden="true"><i /><i /><i /><i /></span>
        {auth.busy ? 'Connecting...' : 'Sign in with Microsoft'}<span aria-hidden="true">&#8599;</span>
      </button>
      {auth.accounts.length > 0 && <div className="choose-account"><p>Or continue with an account on this device</p>
        {auth.accounts.map((account) => <button className="secondary-button" key={account.id} disabled={auth.busy}
          onClick={() => auth.selectAccount(account.id)}>{account.username || account.name}</button>)}
      </div>}
      <div className="auth-footnote"><span className="status-dot" />For authorized users in the configured Microsoft Entra tenant.<br />API access requires the delegated chat.access permission.</div>
    </div>
    <p className="auth-footer">A clearer space for your thinking.</p>
  </main>
}
