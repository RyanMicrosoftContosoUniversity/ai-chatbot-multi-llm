import { useCallback, useState } from 'react'
import { InteractionRequiredAuthError } from '@azure/msal-browser'
import type { IPublicClientApplication } from '@azure/msal-browser'
import { authenticationProblem } from './auth'
import type { AuthSession } from './auth'
import type { PublicConfig } from '../config'

export type AuthClient = Pick<IPublicClientApplication, 'getActiveAccount' | 'getAllAccounts'
  | 'acquireTokenSilent' | 'loginRedirect' | 'acquireTokenRedirect' | 'logoutRedirect' | 'setActiveAccount'>

export function useAuth(client: AuthClient, config: PublicConfig, initialError: string | null = null): AuthSession {
  const [account, setAccount] = useState(client.getActiveAccount())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(initialError)
  const [needsInteraction, setNeedsInteraction] = useState(false)
  const accounts = client.getAllAccounts().filter((item) => item.tenantId === config.tenantId)
  const selectedAccount = account?.tenantId === config.tenantId ? account : null

  const getToken = useCallback(async () => {
    if (!selectedAccount) throw authenticationProblem(new InteractionRequiredAuthError('interaction_required', 'No active account is selected.'))
    try {
      const result = await client.acquireTokenSilent({ account: selectedAccount, scopes: [config.apiScope] })
      return result.accessToken
    } catch (cause) {
      if (cause instanceof InteractionRequiredAuthError
        && client.getActiveAccount()?.homeAccountId === selectedAccount.homeAccountId) setNeedsInteraction(true)
      throw authenticationProblem(cause)
    }
  }, [client, selectedAccount, config.apiScope])

  async function interact(action: () => Promise<void>) {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (cause) {
      setError(authenticationProblem(cause).message)
    } finally {
      setBusy(false)
    }
  }

  const mapAccount = (item: NonNullable<typeof selectedAccount>) => ({
    id: item.homeAccountId, name: item.name || item.username || 'Tenant account', username: item.username,
  })
  return {
    account: selectedAccount ? mapAccount(selectedAccount) : null,
    accounts: accounts.map(mapAccount),
    busy, error, needsInteraction, getToken,
    signIn: () => interact(() => client.loginRedirect({ scopes: [config.apiScope], prompt: 'select_account' })),
    reconnect: () => interact(() => client.acquireTokenRedirect({ scopes: [config.apiScope], account: selectedAccount ?? undefined })),
    signOut: () => interact(async () => {
      // Unmount account-scoped state and abort its requests before leaving the page.
      client.setActiveAccount(null)
      setAccount(null)
      await client.logoutRedirect({ account: selectedAccount })
    }),
    selectAccount: (id) => {
      const next = accounts.find((item) => item.homeAccountId === id)
      if (!next) {
        setError('That account is no longer available. Sign in again.')
        return
      }
      client.setActiveAccount(next)
      setAccount(next)
      setNeedsInteraction(false)
      setError(null)
    },
  }
}
