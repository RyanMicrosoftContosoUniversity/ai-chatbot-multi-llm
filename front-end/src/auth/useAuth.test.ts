import { act, renderHook } from '@testing-library/react'
import { InteractionRequiredAuthError } from '@azure/msal-browser'
import type { AccountInfo, AuthenticationResult } from '@azure/msal-browser'
import { describe, expect, it, vi } from 'vitest'
import { useAuth } from './useAuth'
import type { AuthClient } from './useAuth'
import type { PublicConfig } from '../config'

const config: PublicConfig = {
  tenantId: 'tenant-one', spaClientId: 'spa-one',
  apiBaseUrl: 'https://api.example.test', apiScope: 'api://api-one/chat.access',
}
const account: AccountInfo = {
  homeAccountId: 'home-one', localAccountId: 'local-one', environment: 'login.microsoftonline.com',
  tenantId: config.tenantId, username: 'user@example.test', name: 'Test User',
}
const tokenResult: AuthenticationResult = {
  authority: 'https://login.microsoftonline.com/tenant-one',
  uniqueId: account.localAccountId, tenantId: account.tenantId, scopes: [config.apiScope],
  account, idToken: 'id-token-never-sent-to-api', idTokenClaims: {},
  accessToken: 'api-access-token-test-only', fromCache: true, expiresOn: new Date(),
  tokenType: 'Bearer', correlationId: 'correlation-one',
}
function client(): AuthClient {
  let active: AccountInfo | null = account
  return {
    getActiveAccount: vi.fn(() => active),
    getAllAccounts: vi.fn(() => [account, { ...account, homeAccountId: 'home-two', username: 'other@example.test' }, { ...account, tenantId: 'other-tenant', homeAccountId: 'not-our-tenant' }]),
    setActiveAccount: vi.fn((next) => { active = next }),
    acquireTokenSilent: vi.fn().mockResolvedValue(tokenResult),
    loginRedirect: vi.fn().mockResolvedValue(undefined),
    acquireTokenRedirect: vi.fn().mockResolvedValue(undefined),
    logoutRedirect: vi.fn().mockResolvedValue(undefined),
  }
}
describe('MSAL authentication integration', () => {
  it('acquires the delegated API access token for the active tenant account, not its ID token', async () => {
    const msal = client()
    const { result } = renderHook(() => useAuth(msal, config))
    await expect(result.current.getToken()).resolves.toBe('api-access-token-test-only')
    expect(msal.acquireTokenSilent).toHaveBeenCalledWith({ account, scopes: [config.apiScope] })
    expect(result.current.accounts).toHaveLength(2)
  })

  it('requires an explicit reconnect after interaction-required and never triggers implicit login/replay', async () => {
    const msal = client()
    vi.mocked(msal.acquireTokenSilent).mockRejectedValue(new InteractionRequiredAuthError('interaction_required', 'Consent needed'))
    const { result } = renderHook(() => useAuth(msal, config))
    await act(async () => {
      await expect(result.current.getToken()).rejects.toMatchObject({ code: 'authentication_required' })
    })
    expect(result.current.needsInteraction).toBe(true)
    expect(msal.acquireTokenRedirect).not.toHaveBeenCalled()
    await act(async () => { await result.current.reconnect() })
    expect(msal.acquireTokenRedirect).toHaveBeenCalledWith({ scopes: [config.apiScope], account })
  })

  it('selects only a known tenant account and clears the active account before logout', async () => {
    const msal = client()
    const { result } = renderHook(() => useAuth(msal, config))
    act(() => result.current.selectAccount('not-our-tenant'))
    expect(result.current.error).toContain('no longer available')
    act(() => result.current.selectAccount('home-two'))
    expect(result.current.account?.id).toBe('home-two')
    await act(async () => { await result.current.signOut() })
    expect(result.current.account).toBeNull()
    expect(msal.setActiveAccount).toHaveBeenLastCalledWith(null)
    expect(msal.logoutRedirect).toHaveBeenCalledWith({ account: expect.objectContaining({ homeAccountId: 'home-two' }) })
  })

  it('surfaces redirect errors and lets the user start an account-selecting sign-in', async () => {
    const msal = client()
    const { result } = renderHook(() => useAuth(msal, config, 'Sign-in was cancelled.'))
    expect(result.current.error).toBe('Sign-in was cancelled.')
    await act(async () => { await result.current.signIn() })
    expect(msal.loginRedirect).toHaveBeenCalledWith({ scopes: [config.apiScope], prompt: 'select_account' })
    expect(result.current.error).toBeNull()
  })
})
