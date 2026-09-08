# Meridian frontend

React + TypeScript + Vite, independently hosted on Azure Static Web Apps (SWA).
The browser calls the Container Apps API **directly across origins**. There is
no SWA linked backend, `/api` proxy, client secret, gateway key, auth bypass,
or local mock mode in the deployed application.

## Local development

Use Node.js 22.12+ (or a supported newer LTS) and npm. The lockfile is checked in.
If refreshing dependencies, npm 11 is recommended; npm 10's peer resolver can
fail on the current Vite/Vitest optional dependency graph.

```powershell
Set-Location front-end
npm ci
Copy-Item .env.example .env.local
# Fill in the public values in .env.local.
npm run dev
```

The dev server uses `http://localhost:5173` with a strict port. Start the backend
separately; this project does not run or proxy it. Register the exact local
origin in backend CORS and the SPA redirect URI in Entra. If port 5173 is
occupied, Vite fails instead of silently choosing an unregistered callback port.

| Command | Purpose |
| --- | --- |
| `npm run dev` | Vite development server |
| `npm test` | Vitest parser, API, config, and React component tests; no cloud calls |
| `npm run test:watch` | Interactive local test development |
| `npm run build` | TypeScript checks and production build into `dist` |
| `npm run preview` | Preview built static assets locally, not an SWA emulator |
| `npm run lint` | Scaffold-provided Oxlint |

## Public build-time configuration

Set these values before `npm run build`. All `VITE_*` values are public and
embedded in JavaScript. Changing an SWA runtime setting alone does not change
the built bundle; rebuild for each environment.

| Variable | Value |
| --- | --- |
| `VITE_API_BASE_URL` | Container Apps HTTPS **origin only**, e.g. `https://chat-api.example.com`; no `/api/v1`, path, query, or trailing resource name. HTTP is allowed only for localhost development. |
| `VITE_ENTRA_TENANT_ID` | Configured Entra tenant GUID, not `common` or `organizations` |
| `VITE_ENTRA_SPA_CLIENT_ID` | Client ID of the **SPA** registration |
| `VITE_ENTRA_API_SCOPE` | Full delegated API scope, e.g. `api://<API-client-ID>/chat.access` |

The API client appends `/api/v1`. It sends only the selected alias, current user
message, and (for history) a fresh `clientMessageId` UUID. It never sends a
transcript, system prompt, or user identity header.

## Microsoft Entra sign-in

Use **separate SPA and API application registrations** in the configured tenant.
Expose `chat.access` on the API and grant the SPA that delegated permission.
Configure consent according to tenant policy. The backend independently checks
tenant, issuer, audience, signature, and delegated scope; a signed-in browser
is not proof of authorization.

MSAL Browser 5 uses authorization code + PKCE and a dedicated redirect bridge.
Register these exact URIs as **Single-page application** redirects:

- Local: `http://localhost:5173/auth-redirect.html`
- Deployed: `https://<your-swa-host>/auth-redirect.html`

Do not register only the root URL: the bridge path is required. The logout
return URL is the app's root (`https://<your-swa-host>/`). Login and explicit
Reconnect use top-level redirects, avoiding popup blockers. The callback is a
separate Vite build entry that invokes
`@azure/msal-browser/redirect-bridge`; it does not mount React or call the API.
Do **not** add a Cross-Origin-Opener-Policy header to this callback page.

MSAL initializes and handles redirects before React mounts, caches its tokens
in session storage, restores the active account, and exposes an account chooser
when multiple tenant accounts exist. The client requests an **API access token**
through `acquireTokenSilent`, not an ID token or a Graph token. If interaction is
required, an explicit Reconnect action is shown. It does not silently redirect,
replay a generation, or substitute a token. Signing out or changing accounts
unmounts account-scoped chat state and aborts its requests.

## Static Web Apps deployment inputs

The deployment context is `front-end`; the artifact is `dist`. The build emits
`index.html`, `auth-redirect.html`, hashed assets, and
`staticwebapp.config.json`. No infrastructure or deployment scripts are owned
by this directory.

`public/staticwebapp.config.json` provides SPA fallback and security headers.
It permits same-origin framing for MSAL's silent callback, and login frames
only from the configured public-cloud Entra authority. It does not use SWA
platform authentication or require SWA's `authenticated` role.

**CSP configuration:** the checked-in policy uses `connect-src 'self' https:`
so any configured HTTPS API origin works without a stale hardcoded production
host. This is deliberately less restrictive than an environment-specific
allowlist. Before a hardened release, replace `https:` with the exact API
origin plus `https://login.microsoftonline.com` and any additional required,
verified Entra authentication endpoints. Preserve `'self'` for same-origin
assets/bridge traffic. Do not use inline scripts, `unsafe-eval`, wildcard CORS,
or put credentials in a policy. The shipped policy permits no HTTP API in a
deployed build; localhost HTTP is for Vite development only.

The API must allow the exact frontend origin and methods
`GET, POST, PATCH, DELETE, PUT, OPTIONS`, accept `Authorization` and
`Content-Type`, and expose `X-Request-ID` and `Retry-After`. Credentials/cookies
are omitted. Preview hosts must have deliberate redirect/CORS configuration
and their own public build variables; they must not default to production.

## Chat and persistence behavior

The authoritative wire contract is `..\docs\api-contract.md`.

- Model aliases come only from `GET /api/v1/models`; discovery errors or empty
  results disable inference. No alias or provider fallback is invented.
- With `historyEnabled: false`, each request uses `/chat` and is independent.
  Visible earlier turns are not sent as context. Text exists only in this page
  and is cleared on reload, account change, logout, or New chat.
- With `historyEnabled: true`, the server's saved conversation list is loaded
  on each visit. Open a conversation to read saved messages; both lists support
  opaque continuation tokens. New conversation starts an empty composer and
  creates the server record on first send. Rename and confirmed delete are
  supported. Message pages must finish loading before sending so hidden
  context is not mistaken for an empty history.
- The durable POST contains a new UUID `clientMessageId`. Inference is **never
  retried automatically**, including duplicate IDs (409), network errors,
  token errors, rate limits, or missing terminal events. Reopen/Refresh history
  to inspect a possibly persisted result rather than blindly sending again.
- Persisted assistant messages support helpful/unhelpful feedback. Clicking
  an already-selected rating clears it with `rating: null`; changes are shown
  only after a successful PUT. Server responses restore saved feedback.
- Fetch streaming handles UTF-8, split CR/LF/CRLF and event boundaries,
  multiline data, comments, malformed events, version mismatch, and missing
  terminal `done`. `error` followed by successful `done` is rejected.
- Stop aborts the fetch and reader, preserving partial output as stopped
  locally. Navigation and unmount also clean up. Upstream cancellation is
  best-effort, not a billing guarantee. A missing terminal event is
  **interruption**, not success. A stored `pending` response is displayed as
  unfinalized; refreshing history does not regenerate it.
- Missing provider token usage is **unavailable**, never zero. Provided quota
  is an approximate gateway snapshot, never an exact balance. HTTP 403 alone
  is not labeled as quota exhaustion. Request IDs and retry guidance are
  displayed without automatic retries.
- User and model output render as escaped plain text with line breaks, not
  HTML or Markdown. Do not introduce raw HTML rendering for model output.

## Code map and test seams

`src/api` owns wire types, runtime response validation, fetch, and streaming.
`src/auth` owns real MSAL integration and its callback. `src/features/chat`
owns workspace state, message presentation, and the composer.
`src/features/conversations` owns navigation and durable actions.
`src/components` holds small shared presentation components.

Tests inject the `ChatApi` interface and `AuthSession` into `App`, or a
`TokenProvider`/fetch function into `createApi`. These seams are imports for
tests, not environment-selectable mocks or deployed auth switches. Tests
exercise successful, failed, missing-terminal, aborted, signed-out, account
switching, discovery failure, safe rendering, paginated history, CRUD, and
feedback states. Live tenant sign-in, real SWA headers, cross-origin CORS, and
gateway inference still require a separately authorized deployment smoke test.
