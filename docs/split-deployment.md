# Current deployment runbook: SWA + Container Apps

This is the current implementation guide. It supersedes application hosting,
container/build commands, identity audience examples, and streaming assumptions
in the historical `deployment-guide.md`, `plan*.md`, and `notes.md`.
Resource names and successful calls recorded in those files are not a live
inventory. Preparation does **not** authorize deployments, resource creation,
role grants, paid model requests, or changes to the shared Foundry account.

## 1. Deployment boundary

| Artifact | Host | Deployment input |
| --- | --- | --- |
| `front-end\dist` | Existing Azure Static Web App | Prebuilt static assets only |
| `back-end\Dockerfile` | Existing Azure Container App | Python-only ACR image by manifest digest |
| Existing APIM API | Reused, not deployed by these workflows | Model discovery + streaming OpenAI-compatible chat |
| Key Vault; optional Cosmos | Preconfigured dependencies | Managed identity and data-plane roles |
| Shared Foundry | Read-only to this task | APIM's established routing and identity remain in place |

Do not link ACA as the SWA `/api` backend. The browser uses the ACA origin from
`VITE_API_BASE_URL` directly. SWA's linked API request limit is 45 seconds.
Selecting a direct API does not remove ACA/APIM/provider timeout limits.

The workflows never create Azure infrastructure, edit Entra registrations,
grant roles, configure Key Vault secrets, alter APIM policies, deploy Foundry
models, or change their quota/capacity. A resource owner must complete the
following one-time setup separately before approving a release.

## 2. Identity and dependency setup

Use separate Entra SPA and API registrations in the configured tenant. Expose
delegated `chat.access` on the API, authorize/consent the SPA as appropriate, and
configure the API registration's `api.requestedAccessTokenVersion` to `2`.
The requested scope is `api://<api-client-id>/chat.access`, but accepted access
token `aud` is the **API client-ID GUID**, not that URI. Configure SPA redirect
URIs as `http://localhost:5173/auth-redirect.html` locally and
`https://<swa-or-custom-host>/auth-redirect.html` in deployment. MSAL uses that
static redirect page and returns sign-out to the origin's `/` path. Keep the
redirect page in the built assets; configure each hostname you actually use.
Neither registration needs a client secret for this flow.

Use the APIM API's base URL, including its suffix (for example `/chat-models`),
as `APIM_ENDPOINT`. It must expose `GET /models` and
`POST /chat/completions`. Keep the subscription key server-side and prefer an
application/API-scoped subscription over an all-access subscription. Review
[streaming requirements](streaming-operations.md) before any approved live call.
The BFF must not receive direct Foundry credentials or implement another router.

**Quota compatibility must be confirmed before release.** Upstream 403 is
recognized as quota exhaustion only when JSON `error.code` or top-level `code`
matches `TokenQuotaExceeded`, `QuotaExceeded`, `quota_exceeded`, or
`token_quota_exceeded` (case-insensitive). Other upstream 403 responses become
502 `upstream_rejected`. The documented APIM HTTP status does not establish its
exact JSON error shape. Confirm or normalize that shape at APIM under separate
approval. Any `on-error` normalization must identify the explicitly assigned
token-limit policy ID and a verified quota reason, never blanket-rewrite 403
or permission failures. See the streaming checklist for the required boundary.

### Private image pull: bootstrap before the first private revision

Use a pre-existing **user-assigned managed identity** for the initial deployment:

1. Have the resource owner create/select a user-assigned identity and grant
   image pull permissions on the target ACR **before** creating the private-image
   revision. Wait for RBAC propagation. Do not rely on a future system-assigned
   identity to authorize a private image that must pull before that identity exists.
2. In an ACR using classic registry RBAC, the pull identity needs `AcrPull`.
   In an ABAC-enabled registry, use `Container Registry Repository Reader`,
   scoped/conditioned to the application repository as appropriate. Legacy
   `AcrPull`/`AcrPush` roles do not grant repository access in ABAC mode.
3. ACR must support ARM-audience authentication for managed identity pull.
   Check network reachability from ACA to ACR and its data endpoints. Do not
   enable ACR admin passwords as an authentication workaround.
4. Attach the identity to ACA and set the registry's identity to that identity's
   **resource ID** and server to ACR's actual `loginServer`. Secure registry
   domain names can contain generated suffixes; never derive them as
   `<registry-name>.azurecr.io`.
5. The owner may create ACA directly with the already authorized identity/private
   image, or start with an approved public bootstrap image, attach/authorize the
   user-assigned identity, and only then switch to the private image. Configure
   the settings/probes below before sending real user traffic.

The deployment workflow expects this existing app and registry configuration.
It does not bootstrap resources or make privileged role assignments. Its
preflight detects basic ingress/identity misconfiguration; it does not prove
all RBAC/network prerequisites.

### Key Vault reference and optional Cosmos

Have the owner grant `Key Vault Secrets User` (or equivalent narrow secret-read
permission) to the identity used by ACA's Key Vault reference. Attach that
identity to ACA. Add app secret `apim-subscription-key` of type **Key Vault
reference**, with the real secret URI and selected identity; do not copy secret
values into GitHub or templates. Then set the container environment variable
`APIM_SUBSCRIPTION_KEY` to a **secret reference** to `apim-subscription-key`.
Leave `KEY_VAULT_URL` unset in this platform-injected deployment.

For a user-assigned identity, ACA's platform reference uses its resource ID;
SDK credential selection uses its client-ID GUID in
`MANAGED_IDENTITY_CLIENT_ID`. These are not interchangeable.
Versionless Key Vault references allow platform-managed rotation; versioned
references pin a version. Account for propagation/restart behavior, verify a
new revision after rotation, and never assume a long-running process instantly
adopts a new value.

When history is enabled, pre-create the configured Cosmos database/containers
with the partition layout required by `back-end\app\repositories\chat_repository.py`:
`conversations` uses `/userId`; `messages` uses hierarchical partition keys
`/userId`, `/conversationId` in that order (partition key version 2).
Grant the runtime identity Cosmos **data-plane** read/write permission (for
example Cosmos DB Built-in Data Contributor at the required scope). Azure IAM
Contributor alone does not grant document access. There is no automatic
container creation, arbitrary TTL or retention cleanup in this deployment.
Leave `COSMOS_ENDPOINT` unset for single-turn-only operation.

## 3. Configure the existing Container App

| Setting | Required initial configuration |
| --- | --- |
| Ingress | External HTTPS, target port `8000`, HTTP transport; insecure HTTP disabled |
| Container | One application container; no command/args override of the Docker entrypoint |
| Uvicorn | Exactly one worker; not development `--reload` |
| Scale | Minimum `1`, maximum `1` |
| Revisions | `Single`; no split-traffic multi-revision rollout yet |
| Termination grace | Allow at least 30 seconds for bounded stream/storage cleanup |
| `ENTRA_TENANT_ID` / `ENTRA_API_CLIENT_ID` | Tenant GUID / API registration GUID |
| `APIM_ENDPOINT` | Existing HTTPS gateway API base |
| `APIM_SUBSCRIPTION_KEY` | `secretref` to the Key Vault-backed app secret |
| `APIM_SUBSCRIPTION_HEADER` | `Ocp-Apim-Subscription-Key`, unless the existing gateway explicitly differs |
| `ALLOWED_MODELS` | `gpt-4o,luna,deepseek`, or a reviewed subset of available aliases |
| `STREAM_USAGE_MODELS` | Empty until per-alias capability is verified |
| `CORS_ORIGINS` | Exact SWA/custom-domain HTTPS origins, comma-separated; no paths/trailing slash |
| Optional SDK identity | `MANAGED_IDENTITY_CLIENT_ID` for user-assigned runtime identity |
| Optional history/telemetry | Settings in [backend README](../back-end/README.md) |

Configure ACA probes explicitly; Docker `HEALTHCHECK` is not a substitute:

| Probe | HTTP path/port | Initial delay | Period | Timeout | Failure threshold |
| --- | --- | --- | --- | --- | --- |
| Startup | `/health/live` on 8000 | 1s | 5s | 3s | 30 |
| Readiness | `/health/ready` on 8000 | 5s | 10s | 3s | 3 |
| Liveness | `/health/live` on 8000 | 10s | 30s | 3s | 3 |

These probes contain no tokens, prompts, secret values or provider requests.
Readiness indicates initialized services, not a paid end-to-end model check.
Validate configured ingress timeouts against the default 180-second generation
deadline and realistic first-token latency before increasing it.
One replica has an intentional availability/capacity tradeoff. Revision changes
can temporarily overlap workers; implement shared coordination before scaling
or requiring a globally exact active-generation limit.

## 4. Configure GitHub environments

Create environments **before** running either workflow. Configure required
reviewers, prevent self-review where supported, and restrict deployment branches
to the repository's protected default branch. If your GitHub plan cannot enforce
the required approvals, do not enable these production workflows until an
equivalent protected approval process exists. An environment name in YAML alone
does not create an approval requirement.

Protect workflow changes with code review. Both deployment workflows are manual
`workflow_dispatch` only, require `confirm=true`, and reject non-default-branch
refs. No PR previews, push deployments, Azure login in validation jobs, or paid
model evaluations are configured. Path-scoped CI checks may be skipped for
unrelated changes: do not make absent/skipped path-specific checks unconditional
branch-protection requirements without an aggregation strategy.

### `backend-production`

Configure **environment secrets**, not hardcoded credentials:

| Secret | Value |
| --- | --- |
| `AZURE_CLIENT_ID` | Dedicated GitHub OIDC deployment identity's client-ID GUID |
| `AZURE_TENANT_ID` | Deployment tenant GUID |
| `AZURE_SUBSCRIPTION_ID` | Subscription containing the target resources |

Configure repository or environment **variables** (environment overrides win):

| Variable | Value |
| --- | --- |
| `ACR_NAME` | Existing registry resource name, not its FQDN |
| `ACR_RESOURCE_GROUP` | Registry's resource group |
| `ACR_REPOSITORY` | Application image repository, e.g. `multillm-bff` |
| `ACA_NAME` | Existing container app name |
| `ACA_RESOURCE_GROUP` | Container app's resource group |

Set up the deployment identity's GitHub federated credential with issuer
`https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`,
and exact subject `repo:<owner>/<repository>:environment:backend-production`.
Do not grant subscription Owner or role-assignment permission to this workflow.
It needs read access to ACR metadata, repository push/read/attribute-update
access for the selected repository, and read/update access to the existing ACA
and its revisions. Classic `AcrPush` covers registry data operations; for ABAC
use the appropriate repository writer/contributor rights including attribute
updates, preferably conditioned to this repository. Scope control-plane
permissions to the target resources. Preassign identity attachment permissions
only if required by your existing ACA update policy; the workflow never grants
them. The runtime image-pull identity is separate from this deployment identity.

The backend workflow builds on the GitHub runner (not ACR Tasks), pushes a
full-commit-SHA tag, reads its actual registry digest, locks both tag and manifest
against writes/deletion, and updates **only** the existing ACA image and
min/max replica settings. It waits for the new revision to be ready and calls
the public readiness probe. It does not alter application settings or secrets.
Locked SHA tags are not overwritten: rerunning the same release may fail at the
push. Recover using the recorded digest/revision under a new approval rather
than unlocking or retagging an existing release.

### `frontend-production`

| Kind | Name | Value |
| --- | --- | --- |
| Environment secret | `AZURE_STATIC_WEB_APPS_API_TOKEN` | Deployment token of the existing SWA |
| Variable | `VITE_API_BASE_URL` | ACA HTTPS origin, without `/api/v1` |
| Variable | `VITE_ENTRA_TENANT_ID` | App's tenant GUID |
| Variable | `VITE_ENTRA_SPA_CLIENT_ID` | SPA registration client-ID GUID |
| Variable | `VITE_ENTRA_API_SCOPE` | `api://<api-client-id>/chat.access` |

Vite embeds these variables at **build time**; all four are public. SWA runtime
application settings do not rewrite the static JavaScript bundle. Rebuild after
changing them. Never add gateway keys, Cosmos credentials or a client secret
under `VITE_*`.

The supported `Azure/static-web-apps-deploy@v1` action uses the SWA deployment
token, not the backend's Azure login. Keep that token in the protected
environment and rotate it independently. It installs from `package-lock.json`,
tests and builds, then uploads only `front-end/dist` using `skip_app_build: true`,
`skip_api_build: true`, and an empty API location. This workflow does not link
an API, create a static site, or post PR deployment comments.

## 5. Release and smoke-test sequence

1. Confirm the resource/identity/probe setup, current subscription, permissions,
   SWA origin/redirect URIs, APIM streaming behavior and cost approval. No
   resource values are inferred from historical notes.
2. Review the desired default-branch commit and its local/CI results. Manually
   dispatch **Deploy backend (manual)** with confirmation and approve its
   protected environment. Record the image digest and resulting ACA revision.
3. Configure frontend public variables and matching backend `CORS_ORIGINS`.
   Manually dispatch **Deploy frontend (manual)** and approve its environment.
4. From the actual SWA browser origin, sign in and check model discovery,
   preflight, one explicitly authorized short streamed reply, and Stop. Check
   first-token delivery through ACA and APIM rather than just localhost. Review
   `X-Request-ID`/`Retry-After`, 401, and recognized gateway-limit handling without
   intentionally exhausting paid quotas.
5. If Cosmos is enabled, check reload/pagination, owner isolation, cancellation
   status, duplicate submission and deletion with disposable test conversations.
   Confirm diagnostics have no prompt/completion/token/secret bodies.

Ordinary CI and automatic deployment probes do not invoke models. Live smoke
calls require separate bounded cost approval. Use browser network tools to
verify cross-origin responses; do not replace streaming with polling.

Rollback is separately approved: select a retained known-good ACA revision or
its recorded image digest, and verify readiness and v1 contract compatibility.
Rebuild/redeploy a reviewed frontend rollback commit with the correct public
settings. Do not unlock immutable release tags or delete resources/history as
a rollback mechanism. Locking images retains storage; review retention and
cleanup with the resource owner rather than deleting releases automatically.

## References

- [SWA API limits](https://learn.microsoft.com/azure/static-web-apps/apis-overview)
- [SWA prebuilt asset configuration](https://learn.microsoft.com/azure/static-web-apps/build-configuration)
- [Managed identity image pull](https://learn.microsoft.com/azure/container-apps/managed-identity-image-pull)
- [ACA Key Vault references](https://learn.microsoft.com/azure/container-apps/manage-secrets)
- [ACR image/tag locking](https://learn.microsoft.com/azure/container-registry/container-registry-image-lock)
- [ACR RBAC and ABAC roles](https://learn.microsoft.com/azure/container-registry/container-registry-rbac-built-in-roles-overview)
