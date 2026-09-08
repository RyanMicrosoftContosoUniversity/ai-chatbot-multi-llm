# FastAPI backend

Python 3.12/3.13, an `app` package, and uv's committed `uv.lock`.
The runtime entrypoint is:

```powershell
uv run --frozen uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --workers 1
```

Run it from `back-end\`, after `Copy-Item .env.example .env`, editing the
placeholders, and `uv sync --locked --python 3.12`. Never copy the repository's
historical root `.env` into an image. The root README covers the separate React
development server.

## Project layout

```text
back-end\
  app\
    __init__.py
    main.py
    api\
      __init__.py
      deps.py
      routes\
        __init__.py
        chat.py
        conversations.py
        health.py
        models.py
    core\
      __init__.py
      auth.py
      config.py
      errors.py
      middleware.py
      sse.py
    domain\
      __init__.py
      schemas.py
    prompts\
      system_v1.txt
    repositories\
      __init__.py
      chat_repository.py
    services\
      __init__.py
      apim_client.py
      chat_service.py
      concurrency.py
  tests\
  README.md
  Dockerfile
  .dockerignore
  .env.example
  pyproject.toml
  uv.lock
```

`main.py` assembles the application, client lifetimes, middleware and routers.
`api\deps.py` supplies authentication and application dependencies; route modules
handle HTTP inputs and delegate work. `services\chat_service.py` orchestrates
generation, history, limits, streaming and cancellation. `apim_client.py` owns
the gateway protocol; `concurrency.py` owns process-local admission.
`repositories\chat_repository.py` provides Cosmos conversation/message storage.
`core` contains shared configuration, authentication, errors, request middleware
and SSE transport, while `domain\schemas.py` defines request contracts.

All tests and project/build files stay at the `back-end` project root, outside
the installed `app` package. The distribution remains named `multillm-bff`;
there is no additional `multillm_bff` source directory.

## Settings

`app\core\config.py` is authoritative. Environment variable names are
uppercase versions of its field names. Lists are comma-separated strings, not
JSON arrays. Unknown settings are ignored, so use this table rather than older
names such as `ENTRA_API_AUDIENCE`.

| Setting | Requirement/default |
| --- | --- |
| `ENTRA_TENANT_ID` | Required tenant GUID |
| `ENTRA_API_CLIENT_ID` | Required API application client-ID GUID; expected v2 token audience |
| `ENTRA_REQUIRED_SCOPE` | `chat.access`; must appear in delegated `scp` |
| `APIM_ENDPOINT` | Required HTTPS API base, e.g. `https://your-gateway.azure-api.net/chat-models`; app appends `/models` or `/chat/completions` |
| `APIM_SUBSCRIPTION_KEY` | Runtime secret, or omit entirely to load from Key Vault; an empty value is invalid |
| `KEY_VAULT_URL` | Required when the key is not injected; HTTPS vault URL |
| `APIM_SECRET_NAME` | `apim-subscription-key` |
| `APIM_SUBSCRIPTION_HEADER` | `Ocp-Apim-Subscription-Key`; must match the gateway |
| `ALLOWED_MODELS` | `gpt-4o,luna,deepseek`; nonempty allowlist |
| `STREAM_USAGE_MODELS` | Optional comma-separated subset of `ALLOWED_MODELS`, empty by default; only listed aliases receive `stream_options.include_usage=true` |
| `CORS_ORIGINS` | `http://localhost:5173`; exact HTTPS origins in deployment, no trailing slash or wildcard |
| `MAX_TOKENS_CAP` | `2000`; range 1-16384 |
| `MAX_MESSAGE_CHARS` | `12000`; range 1-100000 |
| `MAX_CONTEXT_CHARS` | `24000`; range 1-200000 |
| `MAX_BODY_BYTES` | `65536`; range 1024-1048576 |
| `MAX_ACTIVE_GENERATIONS` | `8`; range 1-100, process-local |
| `CONNECT_TIMEOUT_SECONDS` | `10`; greater than zero, at most 60 |
| `READ_TIMEOUT_SECONDS` | `90`; greater than zero, at most 300 |
| `GENERATION_TIMEOUT_SECONDS` | `180`; greater than zero, at most 600 |
| `HEARTBEAT_SECONDS` | `10`; greater than zero, at most 30 |
| `COSMOS_ENDPOINT` | Omitted/blank disables history; otherwise HTTPS Cosmos endpoint |
| `COSMOS_DATABASE` | `chatdb` |
| `COSMOS_CONVERSATIONS_CONTAINER` | `conversations` |
| `COSMOS_MESSAGES_CONTAINER` | `messages` |
| `MANAGED_IDENTITY_CLIENT_ID` | Optional user-assigned runtime identity client-ID GUID, not principal ID/resource ID |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Optional runtime telemetry configuration; never a `VITE_*` setting |

Leave `STREAM_USAGE_MODELS` empty until the corresponding provider routes have
been verified to accept the usage option. For example, set
`STREAM_USAGE_MODELS=gpt-4o` only after verifying that alias; `luna` and `deepseek`
will then receive no `stream_options` field. Listing an alias outside
`ALLOWED_MODELS` fails configuration validation. This capability opt-in does not
guarantee a final usage frame: provider errors or cancellation can still leave
usage unavailable.

Blank `COSMOS_ENDPOINT`, `APPLICATIONINSIGHTS_CONNECTION_STRING`,
`KEY_VAULT_URL`, and `MANAGED_IDENTITY_CLIENT_ID` values are treated as unset.
This does not waive the secret-source requirement: if `APIM_SUBSCRIPTION_KEY`
is omitted, a nonblank `KEY_VAULT_URL` is required. An explicitly empty
`APIM_SUBSCRIPTION_KEY` remains invalid, even when a vault URL is configured.

The app validates configuration at startup. It does not turn off authentication
when configuration or credentials fail. Public `/health/live` and
`/health/ready` probes never generate model tokens; readiness is initialization
state, not an end-to-end Foundry availability or quota check.

## Credentials and identity

Local Key Vault access uses `DefaultAzureCredential`, with interactive browser
fallback disabled. Sign in to your approved developer tool before starting the
app; for Azure CLI-based development, run `az login --tenant <tenant-guid>` in
your own terminal only when you need this credential. The identity needs Key
Vault secret-read permission and network access. Do not mount your host Azure
credential directory in a deployed container or expect host CLI sign-in to
magically work inside Docker.

In ACA, configure a **Key Vault reference** as an app secret, then bind
`APIM_SUBSCRIPTION_KEY` to that secret using ACA's `secretref`. The platform
resolves the reference: the literal text `secretref:...` is not the key and must
not be put in a local dotenv file. Set `MANAGED_IDENTITY_CLIENT_ID` when Cosmos
or SDK-based secret loading should use a user-assigned runtime identity. The
platform Key Vault reference and ACR pull each independently specify their
identity by **resource ID**.

The SPA uses authorization code + PKCE and sends a delegated API access token.
The API registration must issue v2 access tokens. The BFF requires the configured
tenant, issuer `https://login.microsoftonline.com/<tenant-guid>/v2.0`, API client-ID
audience, delegated scope, and user object ID. It supplies `x-user-id` to APIM
from the validated user, never from a browser-supplied identity header.

No client secret for either SPA or API registration, provider key, Foundry
endpoint, or direct Foundry access belongs in this backend.

## Package, tests and container

`app\prompts\system_v1.txt` is loaded from the installed `app` package.
The stored prompt-version identifier remains `system-v1`; renaming the file
does not change the prompt content or existing conversation metadata.
Changing a prompt is a backend change and triggers its validation.

```powershell
# From back-end:
uv run --frozen pytest
uv run --frozen ruff check .
# From repository root:
docker build --tag multillm-bff:local .\back-end
```

Docker uses separate dependency-build and runtime stages, frozen non-development
dependency installs, and a non-editable installed package. Only the virtual
environment enters the final Python image. The context allowlist excludes local
environments, credentials, build outputs and frontend assets. UID/GID 10001 runs
Uvicorn on port 8000 with exactly one worker.

The uv version and application dependency lock are pinned. The Python base tag
receives maintenance updates, and isolated package-build tooling follows the
project's build requirements; this is not a bit-for-bit reproducibility claim.
For a byte-identical rebuild, record/pin verified base-image digests and build
tool versions in a reviewed change. Releases are deployed by the **actual ACR
manifest digest**, not by a mutable tag.

Keep ACA minimum and maximum replicas at **one** and use single-revision mode
until shared coordination exists. Rolling revision replacement can still
briefly overlap old/new processes; the in-process capacity guard is not a
globally exact quota or zero-downtime concurrency guarantee.

See [deployment setup](../docs/split-deployment.md),
[streaming operations](../docs/streaming-operations.md) and the
[API contract](../docs/api-contract.md).
