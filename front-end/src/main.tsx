import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'
import { SetupState } from './App'
import { readConfig } from './config'
import { initializeAuth } from './auth/auth'
import { ConnectedApp } from './auth/ConnectedApp'

const rootElement = document.getElementById('root')
if (!rootElement) throw new Error('Application root element is missing.')
const root = createRoot(rootElement)
root.render(<SetupState message="Restoring your Microsoft Entra session..." loading />)

async function start() {
  try {
    const config = readConfig(import.meta.env)
    const auth = await initializeAuth(config)
    root.render(<StrictMode><ConnectedApp client={auth.client} config={config} initialError={auth.error} /></StrictMode>)
  } catch (error) {
    root.render(<SetupState message={error instanceof Error ? error.message : 'The application could not initialize.'} />)
  }
}
void start()
