# Manual Deployment Guide

Companion to `plan.md`. Ordered, dependency-aware build sequence for deploying by hand (portal + `az` CLI), with Terraform deferred.

**Target resource group:** `multi-llm-chatbot-rg`
**Subscription:** `<your-subscription>` — resolve with `az account show --query id -o tsv`

**Reused, not created:**
| Resource | Value |
|---|---|
| Foundry account | `cog-hao-dev-t7h` (`health-agent-orchestrator-rg`, swedencentral) |
| Foundry project | `ai-chatbot-multi-llm` ✅ exists |
| Endpoint | `https://cog-hao-dev-t7h.cognitiveservices.azure.com/` |
| Deployments | `gpt-4o` (150) · `gpt-5.6-luna` (10) · `DeepSeek-V4-Flash` (10) |

---

## Stage 0 — Decisions to make before you click anything

### 0.1 ⚠️ Region: put the resources in **swedencentral**, not eastus

You created `multi-llm-chatbot-rg` in **eastus**. That's not a problem — **a resource group's location only stores its metadata; resources inside it can live in any region.** No need to recreate it.

But you should deploy the *resources* into **swedencentral**, alongside your models:

- **Every request crosses the gateway→model hop.** APIM in eastus calling Foundry in swedencentral pays a transatlantic round trip on every single call, and again on every retry.
- **Semantic caching multiplies it.** A cache lookup adds an embeddings call *and* a Redis query before the model is even reached. Co-located that's a few ms; split across continents it's three Atlantic crossings per prompt.
- **Cross-region egress is billable.** Small, but pointless.
- The tradeoff is a slower first hop from your browser (~100 ms). On a streaming UI, that shifts TTFT by a fraction of what the model itself takes, and it applies once per request rather than per internal hop.

**Verified:** `BasicV2` is available in swedencentral.

### 0.2 ⚠️ Do NOT harden the shared Foundry account

`cog-hao-dev-t7h` has `disableLocalAuth: false` and lives in **another workload's resource group** (`health-agent-orchestrator-rg`).

Normally I'd tell you to set `disableLocalAuth: true` to force keyless. **Don't.** If the health-agent orchestrator authenticates with keys, you'd break someone else's running system. Use managed identity from *your* side (Stage 4.3) and leave the account's own settings alone.

Same reasoning: don't change its network rules, don't rotate its keys, don't add deployments that consume shared quota without checking.

### 0.3 Record everything as you go

You're deploying manually now and moving to Terraform later (`plan.md` §11). The painful part of that migration is rediscovering what you clicked. Keep a running file of resource names, IDs, SKUs, and any non-default setting. A `terraform import` you can actually complete depends on it.

### 0.4 Naming

Pick a convention now and stick to it — suggestion:

| Resource | Name |
|---|---|
| Log Analytics | `log-multillm-swc` |
| App Insights | `appi-multillm-swc` |
| Key Vault | `kv-multillm-<suffix>` (globally unique) |
| Cosmos | `cosmos-multillm-<suffix>` (globally unique) |
| APIM | `apim-multillm-<suffix>` (globally unique) |
| ACR | `crmultillm<suffix>` — ⚠️ **strip the hyphens**: alphanumeric only, so `contoso-university` → `crmultillmcontosouniversity` (§5.1) |
| ACA environment | `cae-multillm-swc` |
| Container app | `ca-multillm-chat` |

---

## Stage 1 — Start APIM first (it's the long pole)

> **Do this before anything else.** APIM Basic v2 takes **30–45 minutes** to provision. Start it, then build everything in Stage 2 while it cooks. Sequencing this last costs you an hour of staring at a progress bar.

**Portal:** Create a resource → API Management

| Setting | Value |
|---|---|
| Resource group | `multi-llm-chatbot-rg` |
| Region | **Sweden Central** |
| Name | `apim-multillm-<suffix>` |
| Organization name / admin email | your details |
| Pricing tier | **Basic v2** |
| Units | 1 |
| Managed identity | **Enable system-assigned** ← needed in Stage 4.3 |

Or:

```powershell
az apim create `
  --name apim-multillm-<suffix> `
  --resource-group multi-llm-chatbot-rg `
  --location swedencentral `
  --sku-name BasicV2 `
  --publisher-name "Ryan Harrington" `
  --publisher-email "<your-email>" `
  --enable-managed-identity true `
  --no-wait
```

Then move straight to Stage 2. Check back with:

```powershell
az apim show -n apim-multillm-<suffix> -g multi-llm-chatbot-rg --query provisioningState -o tsv
```

### ℹ️ Aside — the `AIGateway` tier exists, and I checked it for you

While verifying SKU availability I found a new **AI Gateway tier (preview)**, available in **East US 2 and Sweden Central** — your exact region. It looks tailor-made for this project: one endpoint for all OpenAI-compatible providers, provisions in ~1 minute instead of 45, policies as portal cards instead of XML, and OpenTelemetry token metrics built in.

**I still recommend Basic v2, because AI Gateway tier can't meet your stated requirements yet:**

| Your requirement | AI Gateway tier |
|---|---|
| Semantic caching (R6) | ❌ **Not offered.** Its only policies are content safety, IP filter, token rate limit, request rate limit. |
| Per-user token **quota** (R4/R5) | ❌ Only a **rate limit** ("token throughput during a rolling minute"). No daily/monthly quota, which is the control you actually want. |
| Per-user metering | ⚠️ Runtime access keys are **gateway-scoped** — one key reaches every model and tool. No policy expressions, so no `counter-key` over a JWT claim. |
| Budgeting | ⚠️ **Pricing not announced.** You can't plan around an unknown bill. |
| Stability | ⚠️ Preview, **no SLA**, preview quotas cap keys/models/throughput. |

The card-based policy model is precisely what removes the flexibility you need — `counter-key="@(...oid...)"` and `token-quota-period="Daily"` are expressible in XML and not in a card.

> **Follow-up (see §4.4):** staying on Basic v2 does *not* cost you the unified model API — that feature supports Basic v2. It is simply still rolling out and hasn't reached this instance, so §4.4 builds the same thing by hand. This is not a reason to revisit the tier decision above.

**Worth watching**, though: it's clearly the platform's direction, and it's already in your region. Re-evaluate at GA when pricing lands. Nothing you build now is wasted — your BFF calls an OpenAI-compatible endpoint either way.

---

## Stage 2 — While APIM provisions

### 2.1 Log Analytics workspace

Create a resource → Log Analytics workspace · RG `multi-llm-chatbot-rg` · **Sweden Central** · name `log-multillm-swc`.

```powershell
az monitor log-analytics workspace create `
  -g multi-llm-chatbot-rg -n log-multillm-swc -l swedencentral
```

### 2.2 Application Insights

Must be **workspace-based** and pointed at the workspace above.

```powershell
az monitor app-insights component create `
  --app appi-multillm-swc -g multi-llm-chatbot-rg -l swedencentral `
  --workspace log-multillm-swc --application-type web
```

Save the **connection string** — the BFF and APIM both need it.

```powershell
az monitor app-insights component show `
  --app appi-multillm-swc -g multi-llm-chatbot-rg `
  --query connectionString -o tsv
```

### 2.3 🔴 Cost Management budget — do this now, not later

Per `plan.md` §1a: your 10,000 TPM caps limit spend *rate*; only a budget catches sustained burn. This is the cheapest insurance in the project and takes two minutes.

**Portal:** Subscription → Cost Management → Budgets → Add

- Scope: subscription (or RG, if you want it narrowly scoped)
- Amount: your comfortable monthly ceiling (~$250 covers the ~$185–215 fixed cost from §14 plus token spend)
- Alerts at **50% / 80% / 100%** of forecast and actual, emailed to you

Budgets don't stop spending — they tell you. That's still the difference between a surprise and a decision.

### 2.4 Key Vault

```powershell
az keyvault create `
  -n kv-multillm-<suffix> -g multi-llm-chatbot-rg -l swedencentral `
  --enable-rbac-authorization true
```

Use **RBAC authorization**, not access policies. Grant yourself admin so you can add secrets:

```powershell
$kvId = az keyvault show -n kv-multillm-<suffix> -g multi-llm-chatbot-rg --query id -o tsv
$me   = az ad signed-in-user show --query id -o tsv
az role assignment create --assignee $me --role "Key Vault Secrets Officer" --scope $kvId
```

What lands here later: the APIM subscription key (Stage 6), and — if you add semantic caching — the Redis connection string, which is the one unavoidable key in the design (`plan.md` §10).

### 2.5 Cosmos DB

Serverless, per `plan.md` §7.

```powershell
az cosmosdb create `
  -n cosmos-multillm-<suffix> -g multi-llm-chatbot-rg `
  --locations regionName=swedencentral `
  --capabilities EnableServerless `
  --default-consistency-level Session
```

Database and containers — note the **hierarchical partition key** on `messages`:

```powershell
$acct = "cosmos-multillm-<suffix>"
az cosmosdb sql database create -a $acct -g multi-llm-chatbot-rg -n chatdb

az cosmosdb sql container create -a $acct -g multi-llm-chatbot-rg -d chatdb `
  -n conversations --partition-key-path "/userId"

az cosmosdb sql container create -a $acct -g multi-llm-chatbot-rg -d chatdb `
  -n messages --partition-key-path "/userId" "/conversationId" --partition-key-version 2
```

⚠️ **Hierarchical partition keys cannot be changed after creation.** Getting this right now avoids recreating the container later (`plan.md` §7).

**Then disable key-based auth** to force keyless — safe here, because unlike the Foundry account, you own this resource:

```powershell
az cosmosdb update -n $acct -g multi-llm-chatbot-rg --disable-local-auth true
```

### 2.6 🔴 Cosmos data-plane RBAC — the gotcha that costs people an afternoon

**Cosmos DB has two separate permission systems.** The "Access control (IAM)" blade in the portal grants *control-plane* rights (managing the account). It does **not** grant permission to read or write documents. Data-plane access uses a different role system that the portal barely surfaces — you assign it with the CLI.

If you skip this, your app authenticates successfully and then gets 403 on every query, with an error message that does not point at the cause.

Grant **yourself** access for local development:

```powershell
$acct = "cosmos-multillm-<suffix>"
$me = az ad signed-in-user show --query id -o tsv
$scope = az cosmosdb show -n $acct -g multi-llm-chatbot-rg --query id -o tsv

az cosmosdb sql role assignment create `
  -a $acct -g multi-llm-chatbot-rg `
  --role-definition-id "00000000-0000-0000-0000-000000000002" `
  --principal-id $me --scope $scope
```

`...0002` is the built-in **Cosmos DB Built-in Data Contributor**. You'll repeat this for the container app's managed identity in Stage 7.3.

### 2.7 Entra ID app registrations

Do these before the container app so you have the IDs ready for its configuration.

**a) API registration** (the BFF)
- Azure portal → Entra ID → App registrations → New: `multi-llm-chatbot-api`
- **Expose an API** → Set Application ID URI (accept default `api://<client-id>`)
- **Add a scope**: `chat.access`, admin-consent only, enabled

**b) SPA registration** (the React app)
- New registration: `multi-llm-chatbot-spa`
- **Authentication** → Add platform → **Single-page application**
- Redirect URIs: `http://localhost:5173` for dev, plus your ACA URL once known (Stage 7.2 — come back and add it)
- **API permissions** → My APIs → `multi-llm-chatbot-api` → `chat.access` → **Grant admin consent**

Record: SPA client ID, API client ID, API scope URI, tenant ID.

> Single-user note: you can technically collapse these into one registration. Keep them separate anyway — it costs nothing now and matches how this is actually done, which matters since demonstrating the pattern is part of the point.

---

## Stage 3 — Verify the models directly (before any gateway is involved)

Prove the models work *before* adding APIM. When something breaks later you'll know which layer to blame.

```powershell
$ep  = "https://cog-hao-dev-t7h.cognitiveservices.azure.com"
$tok = az account get-access-token --resource https://cognitiveservices.azure.com --query accessToken -o tsv
$h   = @{ Authorization = "Bearer $tok"; "Content-Type" = "application/json" }

# gpt-4o — Azure OpenAI path
$body = @{ messages = @(@{role="user"; content="Reply with just: OK"}); max_tokens = 10 } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Headers $h `
  -Uri "$ep/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21" -Body $body
```

Then repeat for the other two. ⚠️ **Expect differences** (`plan.md` §2):

- **`gpt-5.6-luna`** — newer models often require **`max_completion_tokens`** instead of `max_tokens`, and may reject non-default `temperature`. If you get a 400, that's the first thing to change.
- **`DeepSeek-V4-Flash`** — it's a Foundry model, not an Azure OpenAI one, so it uses the **`/models` route with the model in the body**, not the deployment name in the path:

```powershell
$body = @{ model = "DeepSeek-V4-Flash"; messages = @(@{role="user"; content="Reply with just: OK"}); max_tokens = 10 } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Headers $h -Uri "$ep/models/chat/completions?api-version=2024-05-01-preview" -Body $body
```

**Write down, per model:** the working path, api-version, token parameter name, and any rejected parameters. This table *is* your unified-API backend configuration in Stage 4.

If you can get a token but get 401/403, you're missing the RBAC in Stage 4.3 — grant it to yourself too.

---

## Stage 4 — APIM configuration (once provisioning completes)

### 4.1 Confirm it's ready

```powershell
az apim show -n apim-multillm-<suffix> -g multi-llm-chatbot-rg --query provisioningState -o tsv
```

### 4.2 Confirm the managed identity

```powershell
az apim show -n apim-multillm-<suffix> -g multi-llm-chatbot-rg --query identity -o json
```

If it's null, enable system-assigned identity in the portal (APIM → Security → Managed identities).

### 4.3 🔑 Grant APIM access to Foundry (keyless)

This is the "keyless auth to Foundry" decision made real.

⚠️ **One role is not enough.** `Cognitive Services OpenAI User` covers the Azure OpenAI deployments (`gpt-4o`, `gpt-5.6-luna`) but **not** DeepSeek. Foundry models served over the `/models` route are Models-as-a-Service and need the data action `Microsoft.CognitiveServices/accounts/MaaS/chat/completions/action`, which that role does not grant. Symptom, verified on this deployment:

```
"code": "PermissionDenied",
"message": "The principal `<apim-mi-object-id>` lacks the required data action
            `Microsoft.CognitiveServices/accounts/MaaS/chat/completions/action`
            to perform `POST /models/chat/completions` operation."
```

**`Cognitive Services User` is the role that works.** Its data action is `Microsoft.CognitiveServices/*`, which covers both the OpenAI and MaaS routes, so it is the only assignment strictly required.

> ⚠️ **`Azure AI Developer` looks correct and is not.** Its data actions include `Microsoft.CognitiveServices/accounts/MaaS/*`, which reads like an exact match — but assigning it produced only a less specific denial (`"Principal does not have access to API/Operation."`) and never succeeded, even after waiting out propagation. Don't spend time on it. If you assigned it while debugging, it is redundant once `Cognitive Services User` is in place.

```powershell
$apimMi = az apim show -n apim-multillm-<suffix> -g multi-llm-chatbot-rg --query identity.principalId -o tsv
$aiId   = az cognitiveservices account show -n cog-hao-dev-t7h -g health-agent-orchestrator-rg --query id -o tsv

# Covers both the /openai/deployments/* and /models routes.
az role assignment create `
  --assignee-object-id $apimMi --assignee-principal-type ServicePrincipal `
  --role "Cognitive Services User" --scope $aiId
```

> **Least privilege note.** `Cognitive Services User` is broader than `Cognitive Services OpenAI User`. If you want to keep the narrower grant for the OpenAI models you may hold both, but it buys nothing: the wildcard supersedes it. What you must not do is assign only the OpenAI role and assume all three models work — two will, and the third fails in a way that looks like a routing bug.

⚠️ **Allow several minutes for propagation.** Data-plane RBAC on Cognitive Services is not immediate; a 401/403 straight after assignment is expected. Re-test before concluding the role is wrong.

⚠️ You're assigning a role on a resource in **another resource group** — you need Owner/UAA rights there. If it fails, that's a permissions issue on the shared account, not a mistake in the command.

**Verify before continuing.** Confirm the assignments actually landed, rather than trusting the create command's output (it returns `roleDefinitionName: null` on success, which is confusing):

```powershell
az role assignment list --scope $aiId --assignee $apimMi `
  --query "[].{role:roleDefinitionName,scope:scope}" -o table
```

### 4.4 Create the unified model API

> ### ⚠️ Read this first — the portal wizard is not available to you
>
> The built-in **unified model API** wizard (APIM → APIs → **Models** → **+ Add** → **Unified model API**) is documented as **APPLIES TO: Developer | Basic | Basic v2 | Standard | Standard v2 | Premium | Premium v2**, so Basic v2 *is* a supported tier. But the feature is **in preview and still rolling out**, and it has not reached this instance.
>
> Verified on `apim-multillm-contoso-university` (BasicV2, Sweden Central):
> - No **Models** node appears under **APIs** in the portal sidebar.
> - The newest api-version registered for `Microsoft.ApiManagement` is `2025-09-01-preview`, which predates the feature.
> - `GET .../models`, `.../llmModels`, and `.../modelRoutes` all return **404 Not Found** on the control plane.
>
> **Do not upgrade the SKU to fix this.** Basic v2 already qualifies, so an upgrade buys nothing. And do **not** switch to the **AI Gateway tier** — §0.98 already rejected it for this project (no semantic caching → fails R6; rate limit only, no daily quota → fails R4/R5; pricing unannounced). Early access via the *AI Gateway Early release channel* is offered only for **classic** tiers; Basic **v2** is not classic, so there is no switch to flip.
>
> Build it by hand instead. That is §4.4a–4.4d below. You lose only two things the wizard would have generated: automatic **format translation** (irrelevant here — all three models speak OpenAI Chat Completions) and the automatic **`/models`** discovery endpoint (six lines of policy, in §4.4d).
>
> When the wizard does appear in your instance, prefer it for new work — but by then this API is already carrying traffic, and there is no reason to migrate it.

#### 4.4.0 Why this API exists (read before building it)

The name "HTTP API" is misleading. You are not writing an API. You are building an **empty façade whose entire value is the policies attached to it**.

**Façade** here is the design pattern in its literal sense: a single simplified interface placed in front of a more complex subsystem, so that callers depend on the interface rather than on the parts behind it. Three heterogeneous endpoints — two Azure OpenAI deployment paths, one Foundry `/models` route, different api-versions, different token parameter names — hide behind one uniform `POST /llm/v1/chat/completions`. The façade holds no logic of its own; it translates and delegates. It is a façade rather than a proxy precisely because it does not merely forward: it presents a *simpler and different* interface than the thing behind it.

**What it replaces.** Without it, the BFF calls Foundry directly. That means the BFF holds a Foundry credential, knows deployment names, knows that DeepSeek uses `/models` while the others use `/openai/deployments/...`, and knows that `luna` wants `max_completion_tokens`. Every one of those facts becomes application code, and every change to them becomes a redeploy.

**What it buys you** — four things, each tied to a requirement:

1. **One endpoint, one credential.** The BFF learns exactly two facts: a URL and a subscription key. `POST /llm/v1/chat/completions` with `{"model": "..."}` is the entire contract. This is why §4.5 calls it the core of the design.

2. **Alias indirection.** `gpt-4o` / `luna` / `deepseek` are *your* names, not Azure's. The React dropdown never learns a deployment name. When `gpt-5.6-luna` is superseded you repoint one `when` branch — no rebuild, no container push. The per-model quirks in §4.4c are absorbed here so the app sees one uniform OpenAI-shaped API.

3. **A governance chokepoint.** This is the real reason. Quota, metering, and safety only work if there is exactly one place every request must pass through. `llm-token-limit` and `llm-emit-token-metric` (Stage 8) have nowhere else to live — you cannot meaningfully rate-limit from inside the application you are trying to constrain, because that application could simply decline to call the limiter. The façade makes the control **unavoidable** rather than voluntary.

4. **Keyless, with a contained blast radius.** APIM's managed identity holds the Foundry access; nothing downstream ever sees a Foundry credential (§4.3). If the container is compromised, the attacker holds an APIM subscription key that is rate-limited, quota-capped, and revocable in one click — not a Foundry key with uncapped spend.

**Why the HTTP tile and not the AI ones.** You are not importing a contract or wrapping an Azure resource — there is no OpenAPI document and no App Service to point at. You need a bare shell with two operations you define, so policies can do the routing. The **Language Model API** and **Microsoft Foundry** tiles each bind to a single backend, which is exactly what this design exists to avoid.

**The mental model:** APIM here is a *policy host that happens to speak HTTP*. The two operations are hooks, not implementations — `POST /chat/completions` carries the routing `choose`, `GET /models` returns a static list. Everything deferred in §9 (semantic caching R6, content safety) later plugs into this same chokepoint with **no application change at all**. That is the payoff for building it now rather than calling Foundry directly and retrofitting governance afterwards.

#### 4.4a Create the three backends

> **Why named Backend entities instead of URLs in the policy?** You could hardcode each URL in a `set-backend-service base-url="..."` and skip this step. Don't. A Backend is a first-class resource, so the endpoint, its credentials, and (later) its circuit-breaker and load-balancing behaviour live in one place that is visible in the portal, greppable in ARM, and importable into Terraform in §11. It also keeps the routing policy about *routing* — the `choose` block reads as a list of aliases, not a wall of URLs — and it is the seam you need if a model ever moves to a different Foundry account or gets a failover pool in front of it.

**Portal:** APIM → **Backends** → **+ Add** (or the CLI below).

Field by field, since most of this form is deliberately left alone:

| Field | Value | What it does |
|---|---|---|
| **Name** | see table below | The backend ID that `set-backend-service backend-id="…"` matches in §4.4c. Immutable — get it right |
| **Backend hosting type** | **Custom URL** | *Azure resource* picks an App Service / Function / Container App via ARM; Foundry accounts aren't offered. *Service Fabric* is a SF cluster with partition resolution |
| **Runtime URL** | see table below | The base. `rewrite-uri` appends to it, so it must stop **before** `/chat/completions` |
| **Add to load-balanced pool** | skip | Creates a *pool* backend spreading traffic across members by priority (failover order) and weight (split ratio) — how you'd put PTU and PAYG deployments behind one alias. You have one deployment per model, and fan-out is by alias, not load |
| **Circuit breaker rule** | skip — see below | |
| **Authorization credentials** | **leave all tabs empty** | §4.4c's `authentication-managed-identity` already sets `Authorization`. Configuring the **Managed Identity** tab too gives two competing sources for one header |

| Backend ID | Alias it serves | Runtime URL |
|---|---|---|
| `aoai-gpt-4o` | `gpt-4o` | `https://cog-hao-dev-t7h.cognitiveservices.azure.com/openai/deployments/gpt-4o` |
| `aoai-luna` | `luna` | `https://cog-hao-dev-t7h.cognitiveservices.azure.com/openai/deployments/gpt-5.6-luna` |
| `foundry-deepseek` | `deepseek` | `https://cog-hao-dev-t7h.cognitiveservices.azure.com/models` |

> **Managed identity: policy or backend?** Both work. Setting **Managed Identity → System assigned**, audience `https://cognitiveservices.azure.com`, on each backend and deleting the policy line is equally correct and more portal-native. This guide keeps it in the policy so the whole keyless path is readable in one place next to the routing. **Pick one — never both.**

> **Circuit breaker rule — what it is, and why not yet.** A per-backend tripwire: after N failures matching given status codes within a time window, APIM marks the backend "open" and returns 503 *immediately* without calling it, then half-opens later to test recovery. The point is to fail fast instead of making every user wait on a timeout, and to stop hammering a sick dependency. It honours `Retry-After`.
>
> Skip it here, and be deliberate if you add it later: your natural failure mode is **429** from the 10,000 TPM cap. Tripping on 429 would take a model entirely offline for the trip duration after one burst — strictly worse than passing the 429 through to a client that can back off. If you do add a rule, scope it to **5xx only**. §8 already handles the 429 case by design: `llm-token-limit` at 7,000 TPM fails first with a clean 429 you control.

Note the third URL has no deployment segment — that is the `/models` route difference you confirmed in Stage 3.

```powershell
$sub  = az account show --query id -o tsv
$base = "https://management.azure.com/subscriptions/$sub/resourceGroups/multi-llm-chatbot-rg/providers/Microsoft.ApiManagement/service/apim-multillm-<suffix>"
$ep   = "https://cog-hao-dev-t7h.cognitiveservices.azure.com"

@{
  "aoai-gpt-4o"      = "$ep/openai/deployments/gpt-4o"
  "aoai-luna"        = "$ep/openai/deployments/gpt-5.6-luna"
  "foundry-deepseek" = "$ep/models"
}.GetEnumerator() | ForEach-Object {
  $body = @{ properties = @{ url = $_.Value; protocol = "http" } } | ConvertTo-Json -Depth 5
  az rest --method put --url "$base/backends/$($_.Key)?api-version=2024-05-01" --body $body
}
```

#### 4.4b Create the API shell

**Portal:** APIs → **+ Add API** → **HTTP** (under *Define a new API*). The dialog opens on **Basic**; switch the toggle to **Full** to see every field below.

| Field | Value | Why |
|---|---|---|
| **Display name** | `Chat Models` | |
| **Name** | `chat-models` | Auto-generated from display name; fine as-is |
| **Description** | optional | |
| **Web service URL** | ⚠️ **leave empty** | See note below |
| **URL scheme** | **HTTPS** | Never `Both` — there is no reason to accept plaintext |
| **API URL suffix** | `llm/v1` | Base URL becomes `https://apim-multillm-<suffix>.azure-api.net/llm/v1` |
| **Tags** | skip | |
| **Products** | leave unselected | See note below |
| **Version this API?** | unchecked | Versioning adds a path/header segment you'd have to thread through the BFF. Not now. |

> **Why `Web service URL` stays empty.** It sets a *default* backend for the API. You don't want one: every branch in §4.4c calls `set-backend-service` explicitly, and the `otherwise` branch returns a clean 400. If you fill this in, an unrecognised alias silently goes to the default backend and fails somewhere upstream instead — exactly the confusing-error-far-from-its-cause problem the `otherwise` branch exists to prevent.

> **Why no Product.** The portal warns "To publish the API, you must associate it with a product." That's fine here. Products exist to bundle APIs for external developer sign-up, which this design doesn't use — the BFF is the only caller. §4.5 authenticates with the **built-in all-access subscription** key, whose scope is all APIs and does not depend on product membership.

> **`Subscription required` is not on this form.** It defaults to **on**, which is what you want. Confirm it after creation under APIs → `Chat Models` → **Settings**. Leave it on: it is the only thing preventing an anonymous caller who learns the gateway URL from spending your tokens.

Then add two operations:

| Display name | Method | URL template |
|---|---|---|
| `Chat Completions` | `POST` | `/chat/completions` |
| `List Models` | `GET` | `/models` |

#### 4.4c The routing policy (this *is* the alias indirection)

⚠️ **Policy scope matters here.** APIM runs **API-level** inbound *before* **operation-level** inbound. Stage 8's `llm-token-limit` and `llm-emit-token-metric` sit at API level and read `modelAlias` — so `modelAlias` must be set at **API level** too, or it is always empty and every user shares one quota bucket.

**First — APIs → `Chat Models` → *All operations* → Inbound processing:**

```xml
<policies>
  <inbound>
    <base />
    <!-- Set at API level so Stage 8's counter-key can see it. -->
    <set-variable name="modelAlias" value="@{
        try { return context.Request.Body.As<JObject>(true)["model"]?.ToString() ?? ""; }
        catch (Exception) { return ""; }
    }" />

    <!-- The client's APIM subscription key must not reach Foundry, or it is
         mistaken for a Cognitive Services key and the call fails 401. -->
    <set-header name="api-key" exists-action="delete" />

    <!-- Stage 8 inserts llm-token-limit and llm-emit-token-metric here. -->
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>
```

The `try/catch` is not decoration — `GET /models` has no body, and an unguarded `As<JObject>` throws there.

**Then — the `Chat Completions` operation → Inbound processing:**

```xml
<policies>
  <inbound>
    <base />

    <!-- Keyless: mints a token for the APIM system-assigned MI granted in 4.3
         and writes the Authorization header. -->
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />

    <choose>
      <when condition="@((string)context.Variables["modelAlias"] == "gpt-4o")">
        <set-backend-service backend-id="aoai-gpt-4o" />
        <rewrite-uri template="/chat/completions?api-version=2024-10-21" />
      </when>

      <when condition="@((string)context.Variables["modelAlias"] == "luna")">
        <set-backend-service backend-id="aoai-luna" />
        <rewrite-uri template="/chat/completions?api-version=2024-10-21" />
        <set-body><![CDATA[@{
            var b = context.Request.Body.As<JObject>(true);
            if (b["max_tokens"] != null) {
                b["max_completion_tokens"] = b["max_tokens"];
                b.Remove("max_tokens");
            }
            b.Remove("temperature");
            return b.ToString();
        }]]></set-body>
      </when>

      <when condition="@((string)context.Variables["modelAlias"] == "deepseek")">
        <set-backend-service backend-id="foundry-deepseek" />
        <rewrite-uri template="/chat/completions?api-version=2024-05-01-preview" />
        <set-body><![CDATA[@{
            var b = context.Request.Body.As<JObject>(true);
            b["model"] = "DeepSeek-V4-Flash";   // route needs the real model name in the body
            return b.ToString();
        }]]></set-body>
      </when>

      <otherwise>
        <return-response>
          <set-status code="400" reason="Bad Request" />
          <set-header name="Content-Type" exists-action="override">
            <value>application/json</value>
          </set-header>
          <set-body>{"error":{"code":"unknown_model","message":"model must be one of: gpt-4o, luna, deepseek"}}</set-body>
        </return-response>
      </otherwise>
    </choose>
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>
```

**Why each piece is there:**

- `As<JObject>(true)` — the `true` is `preserveContent`. Without it the body is consumed and the backend receives nothing.
- The `luna` block encodes the Stage 3 finding directly: `max_tokens` → `max_completion_tokens`, and `temperature` stripped because the model rejects non-default values. **Adjust this to whatever you actually recorded** — if `luna` accepted `max_tokens`, delete the block.
- The `deepseek` block rewrites the alias back to the real deployment name, since the `/models` route reads the model from the body rather than the URL path.
- The `otherwise` branch matters: without it an unknown alias silently falls through to the default backend and produces a confusing error far from its cause. It is also your first working test — an empty request body parses to `modelAlias = ""`, matches no `when`, and returns this 400. If that is what you see in the Test console, the policy chain is working; you simply have not supplied a body yet.
- **`<![CDATA[ ... ]]>` around each `set-body` expression is deliberate.** Those blocks contain `As<JObject>`, and a raw `<` in XML element content is technically invalid markup. APIM's parser tolerates it, but the portal warns *"Some of the policy expressions may not have the correct parentheses or braces format"* on every save. CDATA makes the content literal and silences it. It cannot be used in *attribute* values, which is why the API-level `set-variable value="@{...}"` in the previous block is left as-is.

#### 4.4d The `/models` discovery endpoint

On the **List Models** operation's inbound policy — this replaces what the wizard would have generated, and satisfies the alias-discovery check in 4.5:

```xml
<policies>
  <inbound>
    <base />
    <return-response>
      <set-status code="200" reason="OK" />
      <set-header name="Content-Type" exists-action="override">
        <value>application/json</value>
      </set-header>
      <set-body>{"object":"list","data":[{"id":"gpt-4o","object":"model"},{"id":"luna","object":"model"},{"id":"deepseek","object":"model"}]}</set-body>
    </return-response>
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>
```

> **Token policies still come in Stage 8.** `llm-token-limit` and `llm-emit-token-metric` go at **API level** (not on this operation), so they apply to every alias with one `counter-key`. Content safety and semantic caching remain deferred by decision — and §9.3's ordering constraint is unchanged: content safety must land *before* caching, or an unsafe response gets cached and served repeatedly.

> Aliases are deliberately provider-neutral-ish but readable. If you'd rather decouple further (`fast` / `balanced` / `frontier`), now is the time — the whole point of aliases is that the React dropdown never learns a deployment name. Changing them later means editing the `when` conditions above *and* the `/models` body.

### 4.5 Test through the gateway

⚠️ **`az apim subscription` does not exist.** The CLI has no subscription subgroup — earlier drafts of this guide used one and it fails with *"'subscription' is misspelled or not recognized by the system."* Use the REST API. The built-in all-access subscription's resource **name** is `master` (its *display* name is "Built-in all-access subscription"), and keys require a **POST** to `listSecrets` — a plain GET never returns them.

```powershell
$sub  = az account show --query id -o tsv
$base = "https://management.azure.com/subscriptions/$sub/resourceGroups/multi-llm-chatbot-rg/providers/Microsoft.ApiManagement/service/apim-multillm-<suffix>"

$key = az rest --method post `
  --url "$base/subscriptions/master/listSecrets?api-version=2024-05-01" `
  --query primaryKey -o tsv

$gw = "https://apim-multillm-<suffix>.azure-api.net/llm/v1"

$body = @{ model="gpt-4o"; messages=@(@{role="user";content="Reply with just: OK"}); max_tokens=10 } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri "$gw/chat/completions" -Body $body `
  -Headers @{ "Ocp-Apim-Subscription-Key"=$key; "Content-Type"="application/json" }
```

⚠️ **The header is `Ocp-Apim-Subscription-Key`, not `api-key`.** A plain HTTP API gets the APIM default; `api-key` is what Azure-OpenAI-*imported* APIs use. Confirm yours in the generated OpenAPI under `components.securitySchemes`. If you would rather the OpenAI SDK work against this gateway unmodified, change the header name under APIs → `Chat Models` → **Settings** → *Subscription* to `api-key` and adjust this call to match — but change one or the other, not neither.

> `Content-Type: application/json` is **required**, not decorative. Without it the body reaches the model as untyped bytes and you get `'Input should be a valid dictionary'` — an error that looks like a policy bug but is actually a missing header. The portal **Test** console does not add it for you either.

Then swap `model` to `luna` and `deepseek`. **All three must work here before you write any application code.** Also try `GET $gw/models` to confirm alias discovery.

**Reading the first success.** A 200 proves more than "it works": alias resolution, backend selection, `rewrite-uri`, and — most importantly — **keyless auth**, since no key exists anywhere in the request path. Check that the response contains a `usage` block with `total_tokens`; Stage 8's metering policies parse exactly that, so its presence is what confirms metering will function.

**Verified results on this deployment** — all three aliases and discovery:

| Alias | `response.model` | Notes |
|---|---|---|
| `gpt-4o` | `gpt-4o-2024-08-06` | worked first try |
| `luna` | `gpt-5.6-luna-2026-07-09` | worked first try — **the `max_completion_tokens` rename and `temperature` removal in §4.4c were both accepted** |
| `deepseek` | `DeepSeek-V4-Flash` | required the §4.3 RBAC fix |

`GET /models` returns the three aliases from the §4.4d static policy.

> **If only `deepseek` fails with `PermissionDenied`,** it is §4.3, not your routing. Prove it by calling the `/models` route directly with your own token (the Stage 3 test) — if that succeeds while the gateway does not, the route and body are correct and the gap is APIM's managed identity.

> **A convenience script.** `request.ps1` in the repo root wraps all of the above and takes the alias as an argument: `.\request.ps1 luna`. It prints the reply, the backend model, and the token count. Note the two commands below must stay on **separate lines** — collapsing them onto one produces `ConvertTo-Json : A parameter cannot be found that matches parameter name 'Method'`, because the pipeline swallows `Invoke-RestMethod`'s arguments.

> ℹ️ **Two observations from the first live response, worth recording.**
>
> **1. The façade leaks on the way back.** The response body carries `"model": "gpt-4o-2024-08-06"` — the real deployment, not your alias. Requests are decoupled; responses are not. If the BFF or React ever keys off `response.model`, the coupling §4.4.0 exists to prevent is back. Fix in outbound policy when convenient:
> ```xml
> <outbound>
>   <base />
>   <set-body><![CDATA[@{
>       var b = context.Response.Body.As<JObject>(true);
>       b["model"] = (string)context.Variables["modelAlias"];
>       return b.ToString();
>   }]]></set-body>
> </outbound>
> ```
>
> **2. Content filtering is already on.** Responses include `content_filter_results` and `prompt_filter_results` from Azure OpenAI's built-in filter on the Foundry deployment. That is *not* the same as the `llm-content-safety` policy deferred in §9 — you have baseline per-deployment safety today, and the deferred work is centralised, configurable enforcement at the gateway. This lowers the urgency of §9.3 but does not remove it: the built-in filter is not configurable from APIM and does not cover the semantic cache.

✅ **Milestone:** one endpoint, one credential, three models. This is the core of the design proven.

---

## Stage 5 — Container registry and image

### 5.1 Create the registry

⚠️ **ACR names allow only letters and digits — no hyphens**, 5–50 characters, and must be globally unique across Azure. This breaks the `<suffix>` convention used everywhere else in this guide:

| | |
|---|---|
| ❌ | `crmultillm-contoso-university` |
| ✅ | `crmultillmcontosouniversity` |

**Portal.** The resource type is **Container registries** (plural). It is not obvious from the Create blade — the *Containers* category leads with AKS, Container Apps, and Container Instances, and the registry sits below them.

- Type `Container registries` in the **top search bar** → **+ Create**, or
- **Create a resource** → **Containers** → **Container Registry**, or
- go direct: `portal.azure.com/#create/Microsoft.ContainerRegistry`

**Basics tab:**

| Field | Value | Why |
|---|---|---|
| Resource group | `multi-llm-chatbot-rg` | |
| Registry name | `crmultillmcontosouniversity` | Alphanumeric only, globally unique |
| Location | **Sweden Central** | Must match ACA and APIM. §0.26's argument about cross-region hops applies to image pulls too — a registry in another region slows every cold start and revision rollout |
| Pricing plan | **Basic** | ~$5/mo, 10 GB included. Standard and Premium add only storage, throughput, geo-replication, and private link |

**Why Basic, so you can re-derive it later:**

| | Basic (~$5/mo) | Standard (~$20/mo) | Premium (~$50/mo) |
|---|---|---|---|
| Included storage | 10 GB | 100 GB | 500 GB |
| Geo-replication | ❌ | ❌ | ✅ |
| Private endpoints / VNet | ❌ | ❌ | ✅ |
| Customer-managed keys | ❌ | ❌ | ✅ |
| Availability zones | ❌ | ❌ | ✅ |

One image, one tag, one region — `multillm-chat:v1` is a single container (`plan.md` §8) and will not approach 10 GB. Pulls happen on revision creation, not per request, so throughput is irrelevant. The Premium features don't apply: geo-replication needs multiple regions (you deliberately have one, §0.26) and private endpoints would only matter if ACA were VNet-integrated. Against §14's ~$185–215 fixed cost on a ~$250 budget, Premium would consume a fifth of your headroom for capabilities you can't use.

This is also why **Use availability zones** is greyed out on the form, and why the Networking and Encryption tabs have nothing to configure.

**Upgrading is in-place and non-disruptive** — `az acr update -n <name> --sku Premium`, no re-push, no FQDN change, no downtime — so there is no lock-in risk in starting here. Revisit if you add VNet integration, exceed 10 GB (watch untagged manifests accumulating from repeated `az acr build` runs), or go multi-region.

**Networking** and **Encryption** tabs: leave defaults. Private endpoints and customer-managed keys are **Premium-only**, so Basic offers nothing to change.

**Two Basics-tab fields that are not obvious:**

**Domain name label scope** → **Unsecure** keeps the FQDN as `<name>.azurecr.io`. The other options (Tenant / Subscription / Resource Group Reuse, No Reuse) append a deterministic hash to defend against *subdomain takeover*: if you delete a registry, its globally-unique name is released and anyone in any tenant can recreate it and inherit the FQDN — so anything still pointing there pulls their images.

- **Tenant Reuse** is the best balance if you want the protection: nobody outside your tenant can obtain the FQDN, and the hash is stable, so §11's Terraform destroy/recreate produces the same name.
- **Avoid No Reuse** — a fresh hash on every recreate silently breaks the Container App's image reference each time you rebuild infrastructure.
- ⚠️ If you choose anything but Unsecure, the login server is **not** `<name>.azurecr.io`. Stage 7 hardcodes it in `--image` and `--registry-server`; use the real value instead:
  ```powershell
  az acr show -n crmultillmcontosouniversity --query loginServer -o tsv
  ```
  `az acr build -r <name>` still takes the plain name, so only image references change. This is exactly the non-default setting §0.43 says to write down.

**Role assignment permissions mode** → **RBAC Registry Permissions** (the legacy mode). The alternative, *RBAC Registry + ABAC Repository Permissions*, adds attribute conditions so a role can be scoped to specific repositories (e.g. `Container Registry Repository Reader` where the repository name starts with `team-a/`). Available on **all SKUs, Basic included**.

**Chosen: RBAC-only, deliberately.** ABAC's feature is per-repository scoping, and this project has one repository (`multillm-chat`) and one consumer (the ACA managed identity) — there is nothing to scope. Enabling it would cost two immediate breakages for no present benefit:

| ABAC-enabled consequence | Breaks |
|---|---|
| `AcrPull`, `AcrPush`, `AcrDelete` are **not honored**; use `Container Registry Repository Reader/Writer/Contributor` instead | §7.3 — the grant silently confers nothing and image pull fails |
| ACR Tasks / Quick Builds **lose default data plane access** to the source registry | §5.3 — `az acr build` needs an extra explicit grant |

Also note that under ABAC, `Owner`/`Contributor`/`Reader` become control-plane only and no longer grant data-plane access to images.

> ⚠️ **This is a deferral, not a permanent answer.** Microsoft states ABAC-enabled mode **will become the default** and recommends migrating. Revisit if you add a second image or a CI identity that shouldn't reach everything. Migration is one command — but **order matters**: assign the ABAC-enabled roles *first*, then flip the mode, or you cut off live identities.
> ```powershell
> az acr show   -n crmultillmcontosouniversity --query roleAssignmentMode -o tsv
> az acr update -n crmultillmcontosouniversity --role-assignment-mode rbac-abac
> ```

**CLI equivalent:**

```powershell
az acr create -n crmultillmcontosouniversity -g multi-llm-chatbot-rg -l swedencentral --sku Basic
```

### 5.2 ⚠️ Confirm the admin user is disabled

**Portal:** ACR → **Settings** → **Access keys** → **Admin user** must be **Disabled** (the default).

**Deployed state:** unchecked ✅. This blade is also where you confirm the login server: `crmultillmcontosouniversity-crdsg5h5h5dkb9gm.azurecr.io`.

```powershell
az acr show -n crmultillmcontosouniversity --query adminUserEnabled -o tsv   # expect: false
```

Leave it off. §7.3 grants the Container App's managed identity `AcrPull`, which is the keyless pull path. Enabling the admin user creates a static username/password — precisely the kind of long-lived credential this design eliminates at every other hop (§4.3 for Foundry, §7.3 for Key Vault). It exists for convenience in demos and is the most common way a registry credential ends up in a config file.

**Why it is worse than an ordinary secret:**

| | Admin user | Managed identity + `AcrPull` |
|---|---|---|
| Identity | One shared account for the whole registry — every caller is the same principal | Distinct principal per consumer |
| Permissions | Fixed at **pull + push + delete**, registry-wide. Not adjustable | Least-privilege; `AcrPull` cannot push or delete |
| Rotation | Manual, and rotating breaks every consumer at once | Automatic; tokens are short-lived |
| Attribution | Audit logs show the admin account, not who acted | Logs name the calling identity |
| Exposure | Two passwords that must be stored, transported, and pasted somewhere | Nothing to store |

The permission scope is the part people miss: there is no read-only admin credential. If that password leaks — from a CI variable, a `docker login` in shell history, a screenshot — the holder can **overwrite `multillm-chat:v1` with their own image**. Your Container App then pulls it on the next revision and runs attacker code with your managed identity, which holds Foundry and Key Vault access (§4.3, §7.3). A registry credential is effectively a code-execution credential.

It is also unnecessary here. Nothing in this deployment needs it:

- `az acr build` (§5.3) authenticates as **you**, via your Azure AD login — the ACR task runs server-side and never uses registry credentials.
- The Container App pulls with `--registry-identity system` (§7.2), a managed identity.
- Local `docker push`, if you ever need it, works with `az acr login`, which mints a short-lived AD token rather than using the admin password.

Azure Policy has a built-in **"Container registries should have local authentication methods disabled"** definition, and it's a standard finding in Defender for Cloud and CIS benchmarks — so leaving it off also keeps §12's posture review clean.

The one legitimate use is a system that genuinely cannot do Azure AD — an on-prem build agent or a third-party tool that only accepts a username and password. Even then the better answer is a **repository-scoped token** (Premium) or a service principal, both of which can be scoped and revoked individually. You have neither constraint.

### 5.3 Build the image

Build the single-container image (`plan.md` §8 — React bundle served by FastAPI). ACR builds server-side, so **you don't need local Docker**:

```powershell
az acr build -r crmultillmcontosouniversity -t multillm-chat:v1 .
```

> **There is no portal equivalent for this step.** The portal offers **Tasks**, which build on a git commit trigger — more machinery than this project needs right now. Create the registry in the portal if you prefer, but the build comes back to the CLI.

> **Prerequisite check.** The registry is not the blocker for this stage; a `Dockerfile` and a working app are. If those don't exist yet, creating the registry is harmless but you will be idle until they do. Verify the push landed:
> ```powershell
> az acr repository show-tags -n crmultillmcontosouniversity --repository multillm-chat -o table
> ```

---

## Stage 6 — Store the APIM key

The BFF holds the subscription key server-side (`plan.md` §4). Put it in Key Vault:

```powershell
az keyvault secret set --vault-name kv-multillm-<suffix> --name apim-subscription-key --value $key
```

---

## Stage 7 — Container Apps

### 7.1 Environment

```powershell
$laId  = az monitor log-analytics workspace show -g multi-llm-chatbot-rg -n log-multillm-swc --query customerId -o tsv
$laKey = az monitor log-analytics workspace get-shared-keys -g multi-llm-chatbot-rg -n log-multillm-swc --query primarySharedKey -o tsv

az containerapp env create `
  -n cae-multillm-swc -g multi-llm-chatbot-rg -l swedencentral `
  --logs-workspace-id $laId --logs-workspace-key $laKey
```

### 7.2 Container app

⚠️ **Do not hardcode the login server.** With a domain name label scope other than Unsecure (§5.1) it carries a hash and is *not* `<name>.azurecr.io`. Read it back instead:

```powershell
$acrLogin = az acr show -n crmultillmcontosouniversity --query loginServer -o tsv
# deployed value: crmultillmcontosouniversity-crdsg5h5h5dkb9gm.azurecr.io
```

```powershell
az containerapp create `
  -n ca-multillm-chat -g multi-llm-chatbot-rg `
  --environment cae-multillm-swc `
  --image "$acrLogin/multillm-chat:v1" `
  --registry-server $acrLogin --registry-identity system `
  --system-assigned `
  --ingress external --target-port 8000 `
  --min-replicas 1 --max-replicas 3
```

⚠️ **`--min-replicas 1` is deliberate** (`plan.md` §8). Scale-to-zero means a cold start on the first message of the day — the worst possible first impression for a chat demo.

Then, for streaming, confirm in the portal (Ingress) that the **request idle timeout** is long enough for your longest generation, and set a **termination grace period** so a new revision drains in-flight SSE streams instead of severing them.

Grab the FQDN and **go back to Stage 2.7** to add it as a SPA redirect URI:

```powershell
az containerapp show -n ca-multillm-chat -g multi-llm-chatbot-rg --query properties.configuration.ingress.fqdn -o tsv
```

### 7.3 Grant the app its permissions

```powershell
$caMi  = az containerapp show -n ca-multillm-chat -g multi-llm-chatbot-rg --query identity.principalId -o tsv
$acct  = "cosmos-multillm-<suffix>"
$scope = az cosmosdb show -n $acct -g multi-llm-chatbot-rg --query id -o tsv
$kvId  = az keyvault show -n kv-multillm-<suffix> -g multi-llm-chatbot-rg --query id -o tsv
$acrId = az acr show -n crmultillmcontosouniversity -g multi-llm-chatbot-rg --query id -o tsv

# Cosmos DATA plane (see 2.6 — not the IAM blade)
az cosmosdb sql role assignment create -a $acct -g multi-llm-chatbot-rg `
  --role-definition-id "00000000-0000-0000-0000-000000000002" `
  --principal-id $caMi --scope $scope

az role assignment create --assignee-object-id $caMi --assignee-principal-type ServicePrincipal `
  --role "Key Vault Secrets User" --scope $kvId

az role assignment create --assignee-object-id $caMi --assignee-principal-type ServicePrincipal `
  --role "AcrPull" --scope $acrId
```

### 7.4 Configuration

Set as environment variables (secrets via Key Vault reference):

| Variable | Value |
|---|---|
| `APIM_ENDPOINT` | `https://apim-multillm-<suffix>.azure-api.net/llm/v1` |
| `APIM_SUBSCRIPTION_KEY` | Key Vault reference → `apim-subscription-key` |
| `COSMOS_ENDPOINT` | `https://cosmos-multillm-<suffix>.documents.azure.com:443/` |
| `COSMOS_DATABASE` | `chatdb` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | from 2.2 |
| `ENTRA_TENANT_ID` / `ENTRA_API_CLIENT_ID` / `ENTRA_API_AUDIENCE` | from 2.7 |
| `ALLOWED_MODELS` | `gpt-4o,luna,deepseek` ← server-side allowlist (`plan.md` §4) |
| `MAX_TOKENS_CAP` | e.g. `2000` ← spend control + overshoot bound (§6) |

---

## Stage 8 — Policies (after the path works end to end)

Only now add governance, so failures are attributable.

APIM → APIs → `Chat Models` → ***All operations*** → **Inbound processing** → policy editor.

⚠️ **API level, not the operation** — and it must sit *after* the `set-variable name="modelAlias"` block from §4.4c, in the same policy document. That variable is what `counter-key` and the `Model` dimension read; scope it wrong and both silently collapse to `""`.

```xml
<inbound>
    <base />
    <llm-token-limit
        counter-key="@(context.Request.Headers.GetValueOrDefault("x-user-id","anon") + ":" + (string)context.Variables.GetValueOrDefault("modelAlias",""))"
        tokens-per-minute="7000"
        token-quota="200000"
        token-quota-period="Daily"
        estimate-prompt-tokens="false"
        remaining-tokens-header-name="x-remaining-tokens"
        remaining-quota-tokens-header-name="x-remaining-quota"
        tokens-consumed-header-name="x-tokens-consumed" />
    <llm-emit-token-metric namespace="multillm-chat">
        <dimension name="Model" value="@((string)context.Variables.GetValueOrDefault("modelAlias",""))" />
        <dimension name="User" value="@(context.Request.Headers.GetValueOrDefault("x-user-id","anon"))" />
        <dimension name="ApiId" />
    </llm-emit-token-metric>
</inbound>
```

### 8.1 ⚠️ Enforcement and visibility are separate — keep them in sync

`llm-token-limit`'s `counter-key` gives you per-user **enforcement**. It does *not* give you per-user **visibility**. Those are two different policies reading two different things, and it is easy to ship one without the other:

| Requirement | Mechanism | Without it |
|---|---|---|
| Per-user quota (R4/R5) | `counter-key` on `x-user-id` | One shared bucket — the first heavy user starves everyone |
| Per-user reporting | `dimension name="User"` | You can enforce a limit you cannot explain or audit |

The `User` dimension above closes the second gap. Earlier drafts of this guide had `Model` and `ApiId` only, which meant you could cut off a user for exceeding a quota and then be unable to show them what they had spent.

**Why this matters more once keyless auth is on:** because APIM calls Foundry with its own managed identity (§4.3), Foundry sees exactly one caller for every request in the system. Its metrics blade shows a single undifferentiated stream with no user breakdown, by design. **App Insights is therefore your only source of per-user token data.** If the dimension is missing, the information does not exist anywhere else and cannot be reconstructed after the fact.

**Cardinality — watch the bill.** Custom metric dimensions are billed per unique combination, so cost scales with `User × Model × ApiId`. At single-user and demo scale this rounds to zero. If this ever reaches a real campus population, it will not:

- Emit the Entra **`oid` GUID**, not an email or UPN — stable, lower cardinality, and it keeps PII out of telemetry.
- Consider dropping `ApiId`; with one API it adds a dimension and no information.
- Alerts and dashboards should aggregate. Split by `User` only when investigating a specific case.

**Verify both halves before moving on.** Send a few requests as two different `x-user-id` values, then confirm in App Insights → Metrics that `multillm-chat` splits by `User` — and separately that exceeding `token-quota` actually returns 429 for one user while the other is unaffected. A blank or `anon` dimension means `x-user-id` is not arriving; check the BFF, not the policy.

**Notes:**

- `tokens-per-minute="7000"` sits **below** your 10,000 TPM Foundry cap on purpose (`plan.md` §6) — APIM fails first with a clean 429 you control, rather than leaking Foundry's.
- `token-quota` + `Daily` is the **total** cap that deployment capacity cannot express (§1a).
- ⚠️ The `x-user-id` header above is **placeholder only**. It's trustworthy solely because the BFF sets it after validating the JWT, and APIM is not reachable directly. If you ever expose APIM to browsers, this becomes a trivial impersonation hole — switch to `validate-azure-ad-token` and read the claim in APIM (`plan.md` §4). Note that **both** your quota enforcement *and* your billing attribution (§8.1) now rest on this one header, so spoofing it would let a user both evade their limit and charge their usage to someone else.
- Connect App Insights first (APIM → Monitoring → Application Insights) or `llm-emit-token-metric` has nowhere to write.
- Surface `x-remaining-quota` through the BFF to the UI (`plan.md` §7), and treat it as approximate.

---

## Stage 9 — Deferred

Per decisions in `plan.md`:

| Item | When | Notes |
|---|---|---|
| Semantic caching | Later | Azure Managed Redis **B0** (~$12/mo) + an embeddings deployment. ⚠️ Azure Cache for Redis Basic **will not work** — no RediSearch (§10). Requires access-key auth: the one key in the design. |
| Content safety | Later | Revisit **before** semantic caching ships — a cached unsafe response gets served repeatedly (§9.3). |
| LLMOps / eval | Phase 9 | Script the temporary capacity bump (§1b). |
| Terraform | After it works | Import from your Stage 0.3 notes. |

---

## Build order at a glance

```
1. APIM Basic v2 ─────────────────────────┐  (30–45 min, start first)
2. While waiting:                         │
     Log Analytics → App Insights         │
     Cost budget 🔴                       │
     Key Vault                            │
     Cosmos + data-plane RBAC 🔴          │
     Entra app registrations              │
3. Test models directly (no gateway) ◄────┘
4. APIM: MI → Foundry RBAC, unified API, test all 3 ✅ milestone
5. ACR + image
6. APIM key → Key Vault
7. ACA env + app + RBAC + config
8. Policies: token limit, metrics
9. Deferred: semantic cache, content safety, eval, Terraform
```

## The three things most likely to cost you an afternoon

1. **Cosmos data-plane RBAC (2.6)** — the IAM blade is not enough; the failure is a 403 that doesn't explain itself.
2. **Per-model parameter differences (Stage 3)** — `max_completion_tokens`, temperature restrictions, and DeepSeek's `/models` route instead of `/openai/deployments`.
3. **Starting APIM last** — 45 minutes of dead time that Stage 1 avoids entirely.
