export interface PublicConfig {
  apiBaseUrl: string
  tenantId: string
  spaClientId: string
  apiScope: string
}

export function readConfig(env: Record<string, unknown>): PublicConfig {
  const required = (name: string): string => {
    const value = env[name]
    if (typeof value !== 'string' || !value.trim()) throw new Error(`Missing public setting: ${name}.`)
    return value.trim()
  }
  const apiBaseUrl = required('VITE_API_BASE_URL')
  const tenantId = required('VITE_ENTRA_TENANT_ID')
  const spaClientId = required('VITE_ENTRA_SPA_CLIENT_ID')
  const apiScope = required('VITE_ENTRA_API_SCOPE')
  const guid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
  if (!guid.test(tenantId) || !guid.test(spaClientId)) {
    throw new Error('VITE_ENTRA_TENANT_ID and VITE_ENTRA_SPA_CLIENT_ID must be tenant and SPA application ID GUIDs.')
  }
  const url = new URL(apiBaseUrl)
  const localHttp = url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)
  if ((url.protocol !== 'https:' && !localHttp) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('VITE_API_BASE_URL must be the API HTTPS origin without a path (HTTP is allowed only for localhost).')
  }
  if (!/^(api:\/\/|https:\/\/)\S+\/chat\.access$/.test(apiScope)) {
    throw new Error('VITE_ENTRA_API_SCOPE must be the API application scope URI ending in /chat.access.')
  }
  return { apiBaseUrl: url.origin, tenantId: tenantId.toLowerCase(), spaClientId: spaClientId.toLowerCase(), apiScope }
}
