# Multi-LLM chat: React + FastAPI

Two independently deployed applications use an existing APIM model gateway:

```text
Static Web Apps --> browser (React/MSAL)
                       |
                       | Entra delegated API token + streaming fetch
                       v
                 Container Apps (FastAPI) --> APIM --> existing Foundry models
                       |
                       +--> Key Vault reference; optional Cosmos history
```

Static Web Apps serves **only** the frontend assets. The browser calls the
Container Apps HTTPS origin directly; there is no linked SWA `/api` proxy
(its 45-second API limit is unsuitable for these streams). The backend image
contains Python only, not React or a Node server.

## Start locally

Use Python 3.12 or 3.13, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and Node.js 22. The examples below are PowerShell, run from the repository root.
Keep the applications' `.env` files separate; the historical root environment
template is not the backend's settings reference.

```powershell
Set-Location .\back-end
Copy-Item .env.example .env
uv sync --locked --python 3.12
# Edit .env with your tenant/API registration, APIM API base and Key Vault URL.
# Authenticate the developer credential separately when using Key Vault/Cosmos.
uv run --frozen uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --workers 1
```

In another terminal:

```powershell
Set-Location .\front-end
# Create .env.local with the four VITE_* public settings described below.
npm ci
npm run dev
```

| Frontend public setting | Local example |
| --- | --- |
| `VITE_API_BASE_URL` | `http://localhost:8000` (origin only, no `/api/v1`) |
| `VITE_ENTRA_TENANT_ID` | Your configured tenant GUID |
| `VITE_ENTRA_SPA_CLIENT_ID` | Your SPA registration's client-ID GUID |
| `VITE_ENTRA_API_SCOPE` | `api://<api-client-id>/chat.access` |

Register `http://localhost:5173/auth-redirect.html` as a SPA redirect URI.
The sign-out return URL is `http://localhost:5173/`. Set backend
`CORS_ORIGINS=http://localhost:5173`; using `127.0.0.1` or a different port is a
different origin. The API accepts an Entra **v2 access token** whose `aud` is
the API's client-ID **GUID**, not the `api://...` scope URI. Do not send an ID
token or Microsoft Graph token.

Startup requires valid `ENTRA_TENANT_ID`, `ENTRA_API_CLIENT_ID`,
`APIM_ENDPOINT`, and either a runtime `APIM_SUBSCRIPTION_KEY` or `KEY_VAULT_URL`.
The example deliberately contains no usable credentials. The backend reads its
local `.env` from the working directory. Tests inject dependencies and require
no live Azure access; interactive chat does require the configured services.

## Application behavior

The backend owns the system prompt, allowed model aliases, validated user
identity, request limits and upstream credentials. The browser submits a model
and user message, not an arbitrary transcript or system prompt. Aliases default
to `gpt-4o`, `luna`, and `deepseek`, filtered against gateway discovery.

Single-turn chat works without Cosmos. Configuring `COSMOS_ENDPOINT` enables
durable, user-scoped conversations and feedback; the UI discovers this through
`historyEnabled`. Cosmos databases/containers and permissions must already exist.
Stop aborts the request and closes upstream best-effort; it is not a guarantee
that the provider stops billing. Usage can be unavailable, and gateway quota
metadata is only an approximate snapshot.

```powershell
Set-Location .\back-end
uv run --frozen pytest
uv run --frozen ruff check .
Set-Location ..\front-end
npm run test -- --run
npm run build
```

## Deployment and reference

The repository prepares deployments; it does not provision resources or deploy
on pull requests. Backend/frontend validation workflows are path-scoped. Each
deployment requires a manual dispatch from the repository's default branch and
a separately configured, protected GitHub environment.

- [Backend setup and settings](back-end/README.md)
- [Current split deployment runbook](docs/split-deployment.md)
- [APIM streaming and operational guardrails](docs/streaming-operations.md)
- [Authoritative API/SSE contract](docs/api-contract.md)
- [Deployment preparation plan](.azure/deployment-plan.md)

Older `docs/plan*.md`, `docs/notes.md`, and `docs/deployment-guide.md` preserve
historical decisions and resource observations, not current deployment commands.
Their single-container hosting, older audience examples and response rewriting
must not override the current runbook. Shared Foundry resources remain read-only
to this implementation; neither BFF nor SPA takes direct Foundry credentials.
