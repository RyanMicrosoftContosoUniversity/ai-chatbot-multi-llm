import { useMemo } from 'react'
import type { PublicClientApplication } from '@azure/msal-browser'
import App from '../App'
import { createApi } from '../api/client'
import type { PublicConfig } from '../config'
import { useAuth } from './useAuth'

export function ConnectedApp({ client, config, initialError }: {
  client: PublicClientApplication
  config: PublicConfig
  initialError: string | null
}) {
  const auth = useAuth(client, config, initialError)
  const api = useMemo(() => createApi(config.apiBaseUrl, auth.getToken), [config.apiBaseUrl, auth.getToken])
  return <App auth={auth} api={api} />
}
