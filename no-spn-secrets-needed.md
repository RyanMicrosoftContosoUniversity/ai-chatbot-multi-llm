# Why This System Has No Client Secrets

Companion to `deployment-guide.md` and `plan.md`. A deep dive on the credential model: what a secret actually buys you, why neither app registration needs one, and what replaces them at each hop.

**The short version:** across two app registrations, three Azure data services, an API gateway, and a model endpoint, this design contains exactly **one** long-lived secret — the APIM subscription key — and it lives in Key Vault and is read with a managed identity. Everything else authenticates with cryptographic proofs that are either public, ephemeral, or issued by the platform on demand.

---

## 1. First, what is a client secret *for*?

A client secret answers exactly one question: **"is the application making this request really the application it claims to be?"**

That's it. It is not about the user. It's not authorization. It's application authentication — proving app identity to a token issuer so the issuer will mint a token.

OAuth 2.0 splits clients into two categories on precisely this basis:

| | Confidential client | Public client |
|---|---|---|
| Can keep a credential? | Yes — runs on a server you control | No — code is on the user's device |
| Examples | Server-side web app, daemon, background service | SPA, mobile app, desktop app, CLI |
| Proves identity with | Client secret, certificate, or federated credential | Nothing — it *can't* |

The critical insight is that "public client" is not a weaker configuration you opt into. It's a **factual description of the deployment**. If your code runs in a browser, it is a public client whether you admit it or not. Configuring it as confidential doesn't make it confidential; it just means the secret is now published.

So the question "do I need a secret?" decomposes into two much more answerable ones:

1. **Does this component acquire tokens?** If it only *validates* them, it never talks to the token endpoint, so there is nothing to authenticate to.
2. **If it does acquire tokens, can it hold a credential at all — and is there something better than a secret?**

Applied to this system, the answer to (1) is "no" for the BFF and the answer to (2) is "it can't" for the SPA and "yes, managed identity" everywhere else. That's the whole argument. The rest of this document is the detail.

---

## 2. The SPA: it *cannot* hold a secret, so the protocol stopped asking

### 2.1 The problem with the obvious approach

Suppose you embedded a secret in the React bundle. Its lifetime as a secret is the time it takes someone to open DevTools. It's in the JS bundle served to every visitor, in the browser cache, in your CDN, in any bundle-analysis artifact, and in the source map if you ship one. A "secret" known to every client is a public constant that you have to rotate.

This isn't a theoretical concern — it's why the OAuth 2.0 Security Best Current Practice (RFC 9700) and the browser-app BCP (RFC 8252's spiritual successor for SPAs) both flatly forbid it.

### 2.2 What the authorization code flow needed a secret for

In the classic flow, the browser receives an authorization **code** on the redirect and the server exchanges it for tokens. The code travels through the user's browser, so it's exposed: it's in a URL, in browser history, potentially in a `Referer` header or a proxy log.

The secret was the answer to "what if someone steals the code?" — a stolen code is useless without the secret held server-side. The secret binds redemption to the legitimate application.

For a SPA there's no server in the exchange and no secret, so that binding has to come from somewhere else.

### 2.3 PKCE: proof without a stored credential

**PKCE** (Proof Key for Code Exchange, RFC 7636) replaces the stored secret with a **freshly generated, single-use secret per login**:

```
1. SPA generates a random `code_verifier` (43-128 chars), keeps it in memory
2. SPA computes `code_challenge = BASE64URL(SHA256(code_verifier))`
3. SPA redirects to Entra with `code_challenge` + `code_challenge_method=S256`
   → Entra stores the challenge alongside the pending authorization
4. User authenticates; Entra redirects back with `code`
5. SPA POSTs to the token endpoint: `code` + `code_verifier` (the original, not the hash)
6. Entra hashes the verifier and compares to the stored challenge
   → match: issue tokens.  mismatch: reject.
```

Why this works: the `code_challenge` that traveled over the browser is a **SHA-256 hash**. An attacker who intercepts the code also sees the challenge, but cannot reverse the hash to produce the verifier. Only the browser tab that started the flow holds it, in memory, and it's discarded after one use.

Compare the properties:

| | Client secret | PKCE verifier |
|---|---|---|
| Lifetime | Months, until rotated | One login, seconds |
| Storage | Config, vault, env var | Process memory |
| Scope of compromise | Every session, until rotated | One in-flight login |
| Rotation burden | Real, and often skipped | None — regenerated every time |
| Can be leaked at rest | Yes | There is no "at rest" |

PKCE is strictly better than a secret for this purpose. It is *not* a downgrade accepted because SPAs are limited — it's why the spec now mandates PKCE for confidential clients too. The SPA case simply forced the industry to build the better mechanism.

### 2.4 What the platform choice actually enforced

Selecting **Single-page application** in the Authentication blade (`deployment-guide.md` §2.7b) wasn't cosmetic. It wrote your redirect URI into the manifest's `spa.redirectUris` array rather than `web.redirectUris`, and Entra behaves differently per bucket:

| Behavior | `web` (confidential) | `spa` (public) |
|---|---|---|
| Client secret at `/token` | Required | **Rejected** |
| PKCE | Optional | **Required** |
| CORS headers on `/token` | **Absent** | Present |
| Refresh token lifetime | 90 days, sliding | **24 hours, single-use** |

The CORS row is the one that produces the infamous wasted afternoon: register a SPA's `localhost` URL under **Web** and the login *appears* to work — the redirect happens, the code arrives — and then the token exchange fails in the browser with a CORS error that says nothing about platform types. Entra deliberately omits `Access-Control-Allow-Origin` on the token endpoint for confidential clients, precisely because browser code should never be redeeming a confidential client's code.

The refresh token row matters too: Entra applies stricter rules to SPA-issued refresh tokens because they live in browser storage. 24-hour lifetime, single-use with rotation. Another case of the platform tightening the blast radius rather than trusting storage.

### 2.5 Tokens in the browser: the honest caveat

None of this makes browser-held tokens *safe from the page itself*. If an attacker achieves XSS on your origin, they can read whatever MSAL stored and call your API as the user for the token's lifetime. PKCE doesn't help there — it protects the code exchange, not the resulting token.

The mitigations that do apply:

- **Short access token lifetimes** (Entra default ~60–90 min) bound the window.
- **Session storage over local storage** — MSAL's default `sessionStorage` dies with the tab and isn't shared across tabs.
- **CSP** — the actual defense against XSS, and worth configuring on the FastAPI static-file response.
- **The BFF holds everything that matters.** This is the important one, and it's why the architecture in `plan.md` §4 is shaped the way it is. The browser token grants access to *your chat API*, subject to its allowlist and caps. It does **not** grant access to Foundry, Cosmos, or the APIM subscription key. Those live server-side. A stolen browser token buys an attacker some chat completions inside your token quota — not your data layer.

That containment is a deliberate design property, not a happy accident.

---

## 3. The BFF: it never acquires a token, so it has nothing to prove

This is the part that surprises people, because "my API needs to authenticate" sounds obviously true.

Your BFF is a **resource server** in OAuth terms — it sits at the receiving end. Its job is to answer "is this token real, was it minted for me, and does it permit this operation?" That is a **verification** problem, and verification uses **public** keys.

### 3.1 What validation actually does

Entra signs every JWT with a private key it alone holds. It publishes the matching **public** keys at a well-known endpoint:

```
https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration
   → jwks_uri → https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys
```

The BFF fetches those keys (cached, refreshed on `kid` miss for rotation) and checks, per request:

| Claim | Check | Failure means |
|---|---|---|
| signature | Verifies against Entra's public key for `kid` | Forged or tampered token |
| `iss` | `https://login.microsoftonline.com/<your-tenant>/v2.0` | Token from another tenant |
| `aud` | `api://<api-client-id>` — your `ENTRA_API_AUDIENCE` | **Token minted for a different app, replayed here** |
| `scp` | contains `chat.access` | Token lacks this permission |
| `exp` / `nbf` | within validity window | Expired or not yet valid |
| `tid` | your tenant ID | Cross-tenant token |

Nowhere in that list is a credential belonging to the BFF. **Public keys, public metadata endpoint, no secret.** Asymmetric cryptography is the entire point: the party that verifies does not need the party's signing key.

The `aud` check deserves the emphasis it gets. A bearer token is just a string, and any app in your tenant can obtain valid, correctly-signed, unexpired Entra tokens for *itself*. Without the audience check, a compromised or malicious app could replay one of those at your BFF and be accepted — genuine signature, right tenant, real user. `aud` is what turns "some Microsoft-signed token" into "a token minted specifically for this API."

### 3.2 The claim that becomes your cost control

Validation isn't only a gate. The `oid` claim (the user's immutable object ID) is what the BFF stamps into the `x-user-id` header it sends to APIM — which is the `counter-key` for the per-user token quota in `deployment-guide.md` §8.

That header is trustworthy for exactly two reasons, both structural:

1. The BFF sets it **only after** the validation above succeeds. It is never copied from an inbound request.
2. APIM is not reachable from the browser. The only path to it is through the BFF.

`deployment-guide.md` §8 flags this correctly: expose APIM directly to browsers and `x-user-id` becomes a trivial impersonation hole — send whatever you like and spend someone else's quota. The fix at that point is `validate-azure-ad-token` in APIM, reading the claim itself rather than trusting a header.

### 3.3 When a BFF *would* need a credential

To be precise about the boundary — a BFF needs a credential when it must **acquire** a token, which happens in the **on-behalf-of** flow: the API exchanges the user's token for a *new* token to call a downstream Entra-protected API as that user.

Yours doesn't:

| Downstream | How the BFF reaches it |
|---|---|
| APIM | Subscription key in a header (§4.1 below) |
| Cosmos DB | Managed identity + data-plane role |
| Key Vault | Managed identity + Key Vault Secrets User |
| Application Insights | Connection string (an endpoint + ikey, not an auth secret) |

No OBO, no downstream Entra API, no credential. And even if you added one later, the right answer would be a **federated credential** or managed identity — not a secret.

---

## 4. Everything else: managed identity, and the one exception

### 4.1 The exception, stated plainly

**The APIM subscription key is the single long-lived secret in this design.** APIM's subscription keys are not Entra credentials and have no managed-identity equivalent. So it's handled the way an unavoidable secret should be:

- Stored in **Key Vault** (`apim-subscription-key`, Stage 6)
- Read at runtime via **Key Vault reference** in the container app's config (Stage 7.4)
- Access granted by **managed identity** + `Key Vault Secrets User` (Stage 7.3)
- Never in source, never in a `.env`, never in the image

Note the recursion that managed identity resolves: the secret needs a credential to fetch it, and that credential would itself need storing, and so on. Managed identity terminates the regress — the platform provides the innermost credential, so nothing has to be bootstrapped by hand.

Semantic caching (`plan.md` §10, deferred) would add a second: Azure Managed Redis requires access-key auth. Same treatment — Key Vault, MI-read. Worth noting as a real cost of that feature, not just its $12/month.

### 4.2 How managed identity removes the rest

A **system-assigned managed identity** is a service principal in your tenant whose lifecycle is bound to the Azure resource. Azure holds the credential, rotates it (roughly every 45 days), and never exposes it to you.

The resource obtains tokens from a link-local endpoint reachable only from inside the instance:

```
GET http://169.254.169.254/metadata/identity/oauth2/token
    ?api-version=2018-02-01&resource=https://cosmos.azure.com
Header: Metadata: true
```

`DefaultAzureCredential` in the Azure SDKs does this for you, which is why the BFF's Cosmos client needs an endpoint and no key.

Two properties make it categorically better than a secret:

- **Not exfiltratable.** There's no credential to steal — only a token, and only from inside the running instance, and it expires in ~24 hours. Compare a secret in an env var: readable from a shell, a crash dump, a log line, a compromised CI job, or a screenshot.
- **Automatically rotated.** The rotation you'd otherwise put in a runbook and then not do.

Where this design uses it:

| Principal | Grants | Replaces |
|---|---|---|
| APIM MI | `Cognitive Services OpenAI User` on `cog-hao-dev-t7h` | Foundry API key |
| Container app MI | Cosmos data-plane `00000000-...-0002` | Cosmos account key |
| Container app MI | `Key Vault Secrets User` | Key Vault access policy + key |
| Container app MI | `AcrPull` | ACR admin username/password |

The ACR row is easy to overlook. `--registry-identity system` on `az containerapp create` (Stage 7.2) is what avoids enabling the ACR admin account — a shared static username/password that a great many deployments still ship with.

### 4.3 Why the shared Foundry account is *not* hardened

`deployment-guide.md` §0.2 declines to set `disableLocalAuth: true` on `cog-hao-dev-t7h`. That's not an inconsistency in the keyless story — it's the difference between securing your own resource and breaking someone else's.

The account lives in `health-agent-orchestrator-rg` and belongs to another workload. If that workload authenticates with keys, flipping the switch breaks it. So the boundary is drawn correctly: **your** side of the connection is keyless (APIM's managed identity, §4.3 of the guide), and you leave the shared resource's own configuration to its owner.

Cosmos, which you own outright, does get `--disable-local-auth true`. Same principle, different blast radius.

---

## 5. The complete credential map

```
Browser (React SPA)
  │  no secret — PKCE, tokens in sessionStorage
  │  Authorization: Bearer <JWT aud=api://<api-id> scp=chat.access>
  ▼
BFF (FastAPI on Container Apps)
  │  no secret — validates JWT with Entra's PUBLIC keys (JWKS)
  │  system-assigned managed identity for everything below
  │
  ├─► Key Vault ........ MI + Key Vault Secrets User → reads apim-subscription-key
  ├─► Cosmos DB ........ MI + data-plane role 0002 (local auth DISABLED)
  ├─► ACR (image pull) . MI + AcrPull (admin account never enabled)
  ├─► App Insights ..... connection string (telemetry endpoint, not an auth secret)
  │
  │  Ocp-Apim-Subscription-Key: <from Key Vault>   ◄── THE ONE SECRET
  │  x-user-id: <oid claim, set only after validation>
  ▼
APIM (Basic v2)
  │  system-assigned managed identity
  │  llm-token-limit keyed on x-user-id + model alias
  ▼
Foundry (cog-hao-dev-t7h, shared)
     MI + Cognitive Services OpenAI User — no API key
     account's own disableLocalAuth left alone (§0.2)
```

Secret count: **1**. In Key Vault. Read by managed identity.

---

## 6. Verifying it, rather than believing it

```powershell
# --- No credentials on either app registration ---
az ad app credential list --id <spa-client-id> -o table   # expect empty
az ad app credential list --id <api-client-id> -o table   # expect empty

# --- SPA redirect URI is in the spa bucket, not web/publicClient ---
az ad app show --id <spa-client-id> `
  --query "{spa:spa.redirectUris, web:web.redirectUris, public:publicClient.redirectUris}" -o json

# --- Implicit flow not enabled (both must be false) ---
az ad app show --id <spa-client-id> --query "web.implicitGrantSettings" -o json

# --- Cosmos rejects keys entirely ---
az cosmosdb show -n cosmos-multillm -g multi-llm-chatbot-rg `
  --query "disableLocalAuth" -o tsv        # expect: true

# --- ACR admin account never enabled ---
az acr show -n <acr-name> -g multi-llm-chatbot-rg --query adminUserEnabled -o tsv   # expect: false

# --- Managed identities exist ---
az apim show -n <apim-name> -g multi-llm-chatbot-rg --query identity.principalId -o tsv
az containerapp show -n ca-multillm-chat -g multi-llm-chatbot-rg --query identity.principalId -o tsv

# --- Data-plane assignments (invisible in the portal IAM blade) ---
az cosmosdb sql role assignment list -a cosmos-multillm -g multi-llm-chatbot-rg -o table
```

Inspect a real token at [jwt.ms](https://jwt.ms) during development and confirm `aud`, `scp`, `tid`, and `oid` are what the BFF expects. Paste-decoding a token once is worth more than reading three articles about claims.

---

## 7. Consequences for the Terraform migration

Secretless is *why* `plan.md` §11 is tractable. A design with secrets forces one of three bad outcomes in IaC: secrets in state, a manual out-of-band step that breaks `terraform apply` from clean, or a bootstrap credential that itself needs managing.

Here there's almost nothing to smuggle:

| Resource | Terraform | Secret in state? |
|---|---|---|
| App registrations | `azuread_application` (no `azuread_application_password`) | No |
| Managed identities | `identity { type = "SystemAssigned" }` | No |
| Role assignments | `azurerm_role_assignment` | No |
| Cosmos data-plane roles | `azurerm_cosmosdb_sql_role_assignment` | No |
| APIM subscription key | `azurerm_api_management_subscription` | **Yes — mark sensitive; state must be encrypted** |

The last row is the one to plan for: a remote backend with encryption at rest and restricted access. One row instead of a column is the win.

And keep recording things (`deployment-guide.md` §0.3) — app registrations live in the **tenant**, not the resource group, and Cosmos data-plane assignments don't appear in the portal's IAM blade. Both are invisible to anyone enumerating `multi-llm-chatbot-rg` later, which very much includes future you.

---

## 8. Summary

| Component | Secret? | What it uses instead |
|---|---|---|
| React SPA | No | PKCE — per-login ephemeral proof |
| BFF (token validation) | No | Entra's **public** JWKS keys |
| BFF → Cosmos | No | Managed identity + data-plane role |
| BFF → Key Vault | No | Managed identity + Secrets User |
| BFF → ACR | No | Managed identity + AcrPull |
| BFF → APIM | **Yes** | Subscription key — in Key Vault, read by MI |
| APIM → Foundry | No | Managed identity + Cognitive Services OpenAI User |

Three ideas do all the work:

1. **Verification needs public keys, not secrets.** The BFF validates rather than acquires, so it has nothing to prove and nothing to store.
2. **PKCE beats a stored secret** for a client that can't hold one — ephemeral and per-login rather than long-lived and at rest.
3. **Managed identity terminates the bootstrap regress.** The platform holds and rotates the innermost credential, so the one remaining secret can be stored properly instead of being the thing that guards itself.

A secret you don't have cannot leak, cannot expire at 2am, and cannot be committed to a repository.
