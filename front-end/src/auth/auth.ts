import {
  BrowserCacheLocation,
  InteractionRequiredAuthError,
  PublicClientApplication,
} from '@azure/msal-browser'
import type { PublicConfig } from '../config'
import { ApiError } from '../api/errors'
import type { TokenProvider } from '../api/client'

export interface Account {
  id: string
  name: string
  username: string
}

export interface AuthSession {
  account: Account | null
  accounts: Account[]
  busy: boolean
  error: string | null
  needsInteraction: boolean
  signIn(): Promise<void>
  signOut(): Promise<void>
  reconnect(): Promise<void>
  selectAccount(id: string): void
  getToken: TokenProvider
}

export async function initializeAuth(config: PublicConfig): Promise<{ client: PublicClientApplication; error: string | null }> {
  const client = new PublicClientApplication({
    auth: {
      clientId: config.spaClientId,
      authority: `https://login.microsoftonline.com/${config.tenantId}`,
      redirectUri: `${window.location.origin}/auth-redirect.html`,
      postLogoutRedirectUri: `${window.location.origin}/`,
    },
    cache: { cacheLocation: BrowserCacheLocation.SessionStorage },
  })
  await client.initialize()
  let redirectError: string | null = null
  try {
    const result = await client.handleRedirectPromise()
    if (result?.account?.tenantId === config.tenantId) client.setActiveAccount(result.account)
  } catch (error) {
    // A rejected/cancelled sign-in is a recoverable auth state, not a broken app configuration.
    redirectError = authenticationProblem(error).message
  }
  const accounts = client.getAllAccounts().filter((account) => account.tenantId === config.tenantId)
  if (!client.getActiveAccount() && accounts.length === 1) client.setActiveAccount(accounts[0])
  return { client, error: redirectError }
}

export function authenticationProblem(error: unknown): ApiError {
  if (error instanceof InteractionRequiredAuthError) {
    return new ApiError({
      code: 'authentication_required',
      message: 'Your session needs attention. Use Reconnect to sign in or grant the API permission, then send again. Nothing was retried.',
    })
  }
  return new ApiError({
    code: 'authentication_failed',
    message: error instanceof Error ? `Authentication failed: ${error.message}` : 'Authentication failed. Please sign in again.',
  })
}
