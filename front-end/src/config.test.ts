import { describe, expect, it } from 'vitest'
import { readConfig } from './config'

const env = {
  VITE_API_BASE_URL: 'https://api.example.test',
  VITE_ENTRA_TENANT_ID: '11111111-1111-1111-1111-111111111111',
  VITE_ENTRA_SPA_CLIENT_ID: '22222222-2222-2222-2222-222222222222',
  VITE_ENTRA_API_SCOPE: 'api://33333333-3333-3333-3333-333333333333/chat.access',
}
describe('public configuration', () => {
  it('requires all settings and a tenant-specific authority', () => {
    expect(readConfig(env).apiBaseUrl).toBe(env.VITE_API_BASE_URL)
    expect(() => readConfig({ ...env, VITE_ENTRA_TENANT_ID: 'common' })).toThrow('GUID')
    expect(() => readConfig({ ...env, VITE_ENTRA_SPA_CLIENT_ID: '' })).toThrow('Missing')
  })
  it('requires an HTTPS origin outside local development and a chat.access scope', () => {
    for (const base of ['http://api.example.test', 'https://api.example.test/api', 'https://api.example.test?token=secret']) {
      expect(() => readConfig({ ...env, VITE_API_BASE_URL: base })).toThrow()
    }
    expect(readConfig({ ...env, VITE_API_BASE_URL: 'http://localhost:8000' }).apiBaseUrl).toBe('http://localhost:8000')
    expect(() => readConfig({ ...env, VITE_ENTRA_API_SCOPE: 'User.Read' })).toThrow('chat.access')
  })
})
