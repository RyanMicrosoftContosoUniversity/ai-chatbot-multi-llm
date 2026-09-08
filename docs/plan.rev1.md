# Multi-LLM Chatbot on Microsoft Foundry — Design Notes

> **Historical first design revision, not an implementation runbook.**
> Its single-container/non-streaming-first recommendations are superseded by
> [the current README](../README.md), [split deployment](split-deployment.md)
> and [API contract](api-contract.md). Do not copy this document back to the
> root or use it to provision or modify shared resources.

> **Destination:** on approval, copy to `C:\Users\rharrington\repos\ai-chatbot-multi-llm\plan.md` (repo root, as requested).

**Status:** design discussion. Nothing is being implemented yet.
**Owner:** Ryan Harrington
**Scope decisions so far:** Entra ID (workforce) auth · Python/FastAPI backend · demo/POC, small internal user set · public internet networking · multi-tenancy explicitly out of scope.

---

## 1. What you asked for, restated

| # | Requirement | Notes |
|---|---|---|
| R1 | React front end, prompt in / answer out | |
| R2 | List box to pick the model; selection drives the backend | Claude Opus 4.5, GPT-4o, Qwen3 |
| R3 | Models hosted in / fronted by Microsoft Foundry | |
| R4 | APIM tracks token consumption **per signed-in user** | |
| R5 | Each user gets their own subscription token so limits can be applied | |
| R6 | Semantic caching wherever possible | |
| R7 | "Token caching" wherever possible | interpreted as prompt caching — see §6 |
| R8 | Conversations stored in Cosmos DB and retrievable | |
| R9 | Public internet networking is acceptable | no Private Link / VNet injection |

---

## 2. Headline finding — this changes the design

APIM now has a **unified model API (preview)**. It exposes multiple LLM backends behind **one OpenAI-compatible endpoint** (`/llm/v1/chat/completions`), performs **request/response format translation** between OpenAI Chat Completions and Anthropic Messages, and lets you attach governance policies once for all models. Clients switch models by changing the `model` string to a configured **alias**.

Why this matters to you: your three models do **not** share a wire protocol.

- GPT-4o → OpenAI Chat Completions
- Claude Opus 4.5 → Anthropic Messages API (different body shape, `max_tokens` required, system prompt is a top-level field, different content-block structure, different streaming event names)
- Qwen3 → depends entirely on how it's deployed in Foundry (see §3 open item)

Without the unified API you write and maintain an adapter layer in FastAPI for each provider. With it, APIM owns translation and your backend speaks one dialect.

**Two hard constraints that fall out of this — these drive your SKU choice:**

1. The unified model API applies to **Developer, Basic, Basic v2, Standard, Standard v2, Premium, Premium v2**. It is **preview**; on classic tiers you need the *AI Gateway Early* release channel.
2. The AI gateway LLM policies (`llm-token-limit`, `llm-semantic-cache-lookup/store`, `llm-emit-token-metric`) support the **Anthropic Messages API only in the v2 tiers**.

→ **Recommendation: Basic v2 or Standard v2.** Standard v2 if you want workspaces/more scale headroom; Basic v2 is the cheaper POC option. A classic Developer-tier instance is the obvious "it's just a POC" reflex and it is the wrong call here — you'd lose Anthropic support in exactly the policies (token limiting, semantic cache) that are the point of the project.

**Decision to confirm:** unified model API (preview) vs. hand-rolled adapters in FastAPI. Preview means terms-of-use caveats and possible breaking changes; the adapter route is more code but fully GA. My lean is unified model API, with the backend written so an adapter layer *could* be reintroduced (i.e. don't leak APIM-specific shapes into your domain model).

---

## 3. Open items on the models themselves — verify before building

These are the kind of thing that quietly derails a build in week two.

- **Exact model identifiers.** You wrote `qwen-qwen3.6-27b`. Confirm the exact catalog name, and confirm all three are available in a single region with quota. Claude Opus 4.5 and Qwen3 will not necessarily be co-located with your GPT-4o deployment.
- **Deployment type per model.** GPT-4o is a standard Foundry/AOAI deployment. Claude and Qwen may be serverless (pay-go, model-as-a-service) or managed-compute deployments, each with different endpoint shapes, auth, quota model, and cost profile. Managed compute in particular bills for the *hosting hours*, not per token — which breaks the mental model of "APIM meters my spend," because idle managed compute costs money even when nobody chats.
- **Whether Qwen3 is OpenAI-compatible.** If it is, it slots into the unified API as an OpenAI-format backend. If it isn't, it is your one true adapter case.
- **Per-model parameter differences** you'll have to normalize or expose: `max_tokens` required vs optional, temperature ranges, system-prompt handling, stop sequences, reasoning/thinking modes, context window sizes (this one matters for §7 history trimming).
- **Regional availability and quota (TPM).** Check before you design, not after.

---

## 4. The call path — what "BFF" means and the trade-offs

**BFF = Backend For Frontend.** It's a small server-side API that exists solely to serve one front end. The browser talks only to it; it talks to everything else. It's not a microservice for reuse and not a general API gateway — it's a per-frontend shim that (a) holds secrets the browser must never see, (b) aggregates/reshapes calls so the UI makes one request instead of five, and (c) is the place to enforce authorization decisions you cannot trust a client to make. In your case, FastAPI is the BFF.

The core problem it solves for you: **an APIM subscription key is a bearer credential.** Anything in a browser — JS bundle, `localStorage`, network tab, devtools — is readable by the user and by anything running on the page. If you ship each user their own subscription key to the SPA, you have shipped every user a copy of their own quota-bearing secret, which they can extract, script against outside your UI, and share. Your per-user limit becomes a per-user *suggestion*.

### Option A — BFF (recommended)

```
React SPA ──(Entra ID access token)──► FastAPI (Container Apps)
                                          │  validates JWT, extracts oid
                                          │  loads history from Cosmos
                                          ▼
                                        APIM  ──(managed identity)──► Foundry
                                          │  token limit / quota per user
                                          │  semantic cache (Azure Managed Redis)
                                          │  emit token metrics → App Insights
```

- Subscription key lives in the container's config/Key Vault, never in the browser.
- You control the conversation history server-side, which closes a real hole: if the client sends the full message array, a user can forge prior assistant turns, strip your system prompt, or replay someone else's context. Rehydrate history from Cosmos using the `oid` from the validated token instead.
- Model selection can be validated against an allowlist rather than trusted.
- Cost: one more hop of latency, one more thing to host, and streaming has to be proxied through it (§5).

### Option B — SPA → APIM directly, Entra JWT, no subscription key

APIM validates the JWT (`validate-azure-ad-token`) and the token-limit `counter-key` is a policy expression over the `oid`/`sub` claim rather than `context.Subscription.Id`. You still get exact per-user limits. You lose: the APIM *products* model (no per-user subscription objects, no self-service developer portal semantics), server-side history integrity, and the ability to keep any secret at all. CORS and preflight handling move into APIM.

### Option C — SPA holds a per-user subscription key

Not recommended, for the reason above. The only scenario where it's defensible is a trusted-developer audience calling the API from scripts rather than a browser — which is actually the scenario APIM subscriptions were designed for.

### The per-user subscription question (R5)

There's a tension worth naming: **"each user gets their own subscription key" and "the key never reaches the browser" are compatible, but only via the BFF**, where the BFF looks up the caller's subscription key and attaches it server-side. That's a real pattern and it gives you the APIM products/quotas model. It costs you:

- A **provisioning path**: on first sign-in, create an APIM subscription for that user (APIM Management REST API or ARM), store the mapping `oid → subscriptionId`, cache the key. Plus deprovisioning when someone leaves.
- A **secret-sprawl problem**: N user keys the BFF must store and retrieve (Key Vault, or Cosmos with the key encrypted — not plaintext).
- Operational overhead that, for a small internal POC, is significant relative to its value.

**The simpler equivalent:** one product/subscription for the app, and a `counter-key` built from the caller's identity claim — e.g. `counter-key="@(context.Request.Headers.GetValueOrDefault("x-user-id"))"`, or better, a claim extracted from a JWT that APIM itself validated. You get per-user rate limits and quotas with zero provisioning. What you *don't* get is per-user *products* (i.e. different tiers with different limits), unless you encode tier in the policy expression.

> **Open question for you:** is "each user gets a subscription" a hard requirement (e.g. you want the APIM developer portal, per-user tiers, or key revocation per user), or is it a means to the end of "per-user token limits"? If the latter, use the counter-key approach and save yourself the provisioning machinery. I'd default to counter-key for the POC and note per-user subscriptions as a later step.

⚠️ **Do not derive the counter key from a header the browser controls.** It must come from a claim in a token that APIM or the BFF has validated, or a user can set `x-user-id: someone-else` and spend another person's quota.

---

## 5. Streaming — the deep explanation you asked for

### What it is

A non-streaming call is request/response: you POST the prompt, the connection sits idle while the model generates the *entire* answer, then the whole JSON arrives at once. For a long answer from a large model this is commonly 10–60+ seconds of a spinner.

A streaming call sets `"stream": true`. The model emits tokens as it generates them, and the server pushes each one to the client immediately over a single long-lived HTTP response using **Server-Sent Events (SSE)** — `Content-Type: text/event-stream`, a sequence of `data: {...}\n\n` chunks, terminated by `data: [DONE]`. Each chunk carries a delta (a token or few). The UI appends deltas as they land, producing the familiar typewriter effect.

The user-visible metric that matters is **time-to-first-token (TTFT)**, and streaming collapses it from "the whole generation time" to "a few hundred ms." Total time is unchanged — sometimes marginally worse — but *perceived* responsiveness is dramatically better, and the user can start reading, or hit stop, immediately.

### What it costs you architecturally

1. **Every hop must not buffer.** SPA ← ACA ingress ← FastAPI ← APIM ← Foundry. Any component that buffers the full response to inspect it destroys the effect. FastAPI needs `StreamingResponse` over an async generator with an async HTTP client (`httpx.AsyncClient.stream`) — a sync client or a `list()` over the iterator silently re-buffers it.
2. **Token accounting degrades.** This is the important one, and it's documented behavior: with `stream: true`, `llm-token-limit` **always estimates prompt tokens regardless of the `estimate-prompt-tokens` setting, and estimates completion tokens too.** So your per-user metering (R4) becomes approximate under streaming. Mitigations: request `stream_options: {"include_usage": true}` where the provider supports it so the final chunk carries exact usage, and have the BFF record that exact figure to Cosmos/App Insights as your source of truth for reporting — while APIM's estimate remains the enforcement mechanism. Accept that *enforcement* is estimate-based; make *reporting* exact.
3. **Errors arrive mid-stream.** Once you've sent a 200 and started streaming, you cannot retroactively send a 429. A quota trip mid-generation, or a backend failure at token 400, has to be surfaced as an in-band error event that your React client explicitly handles. Design the event envelope for this up front (e.g. `event: error` alongside `event: delta`).
4. **Persistence timing.** The assistant message can only be written to Cosmos when the stream completes. Handle client disconnect: if the user closes the tab at token 50, you were still billed for the full generation — decide whether to persist the partial message and how to mark it.
5. **Interaction with semantic caching.** A cache hit produces a complete response with no upstream call. Your BFF must be able to synthesize a stream from a whole response so the client sees one consistent protocol. (Chunk it artificially, or send it as a single delta.) Verify how `llm-semantic-cache-store` behaves with streamed responses in your tier before relying on caching + streaming together — this is a specific thing to test in the spike.
6. **Cancellation.** Streaming makes a Stop button possible, which means propagating client disconnect through FastAPI → APIM → Foundry to actually stop billing. Worth doing; not free.

### Recommendation

Build streaming in from the start, but **build the non-streaming path first**. Non-streaming gives you exact token usage and a far simpler debugging surface while you're validating APIM policies, model wiring, and Cosmos persistence. Add streaming as a second, deliberate phase once the plumbing is proven — retrofitting it into a working system is much easier than debugging policy behavior through a stream.

---

## 6. The two caches — they are different things and you want both

You said "semantic caching and token caching." These are unrelated mechanisms with different owners, and conflating them causes design mistakes.

### 6a. Semantic caching (APIM-owned, saves whole calls)

`llm-semantic-cache-lookup` / `llm-semantic-cache-store`. On each request APIM embeds the prompt, does a vector similarity search against previously cached prompts, and if a prior prompt is within `score-threshold`, returns that stored response **without calling the model at all**. Zero tokens, near-zero latency.

Requirements and sharp edges, from the policy docs:

- **Azure Managed Redis** (or another RediSearch-compatible cache) onboarded to APIM as an external cache, plus an **embeddings model deployment** registered as an APIM backend, authenticated with the APIM **system-assigned managed identity** (`embeddings-backend-auth="system-assigned"` — it's the only allowed value).
- `score-threshold` is inverted from intuition: **lower means stricter**. Docs say start around **0.05**; **above 0.2 risks mismatches**. This is the single highest-risk knob in your design — too loose and you serve a user a confidently wrong answer to a question they didn't ask.
- **`vary-by` is a security control, not just a perf knob.** Without partitioning by user, User B can receive a cached completion generated from User A's prompt. Docs explicitly call this out. **You must `vary-by` at minimum the model alias and the user identity** — the model, because a cache hit that ignores the model selector would silently defeat R2 (user picks Claude, gets a cached GPT-4o answer). Per-user partitioning also destroys most of your hit rate, which is the honest trade: cross-user caching is where the savings are, per-user caching is where the safety is. For an internal POC with a shared, non-sensitive corpus you might cache per model + per group rather than per user — an explicit, documented risk decision.
- `ignore-system-messages="true"` is recommended, and `max-message-count` skips caching once a dialog is deep — because **semantic caching fundamentally fits one-shot Q&A far better than multi-turn chat**. "What about the second one?" is semantically similar to a thousand other prompts and means nothing without its history. Expect low hit rates on turn 5+; set `max-message-count` low (e.g. 2–4).
- Add a `rate-limit` policy right after the lookup so a cache outage doesn't stampede the backend, and consider `llm-content-safety` with prompt shields.
- **Reporting side effect:** a cache hit consumes no tokens, so it never increments the token counters. Your per-user usage dashboard will show cache hits as free — which is true, but you'll want to count *hits* separately so "usage" isn't confused with "cost."

### 6b. Prompt caching (model-provider-owned, makes calls cheaper)

Distinct feature, at the model, not the gateway. The provider caches the *processed prefix* of your prompt so repeated leading content isn't re-processed. You still call the model and still get a fresh completion; the input tokens covered by the cache are billed at a large discount and TTFT drops.

- **Anthropic (Claude):** explicit — you mark `cache_control` breakpoints on content blocks, with minimum-token thresholds and a short TTL. Requires deliberate structuring.
- **Azure OpenAI (GPT-4o):** automatic prefix caching above a token threshold; no request changes needed, but it only fires on **exact, stable prefixes**.
- **Qwen3:** likely unsupported. Verify.

The design consequence for both: **put static content first and keep it byte-stable.** System prompt, then any fixed context, then conversation history oldest-to-newest, with the new user turn last. Anything that varies per request (timestamps, a randomized greeting, reordered history, a per-request UUID) at the front of the prompt destroys the prefix and silently costs you the discount. That's a real constraint on how you assemble prompts in the BFF.

⚠️ **Verify:** whether APIM's unified-API format translation preserves Anthropic `cache_control` blocks. If translation strips them, explicit Claude prompt caching may require bypassing the unified path for that model. Test this in the spike — it's exactly the kind of preview-feature gap worth finding early.

---

## 7. Cosmos DB design (R8)

**Recommendation: one document per message, plus one lightweight conversation document.**

The tempting alternative — one document per conversation with a `messages` array — is simple until it isn't: Cosmos has a **2 MB document limit**, and every appended turn rewrites and re-charges RUs for the *entire* growing document. A long chat becomes progressively more expensive to append to and eventually fails outright.

```
Container: conversations     PK: /userId
  { id: <conversationId>, userId, title, createdAt, updatedAt,
    lastModel, messageCount, totalTokens }

Container: messages          PK: /conversationId   (or hierarchical /userId, /conversationId)
  { id: <messageId>, conversationId, userId, role, content,
    model, promptTokens, completionTokens, cachedTokens,
    cacheHit: bool, latencyMs, createdAt, seq }
```

- **Partition key**: `/conversationId` on messages gives you a single-partition read for "load this chat" — the dominant query. Hierarchical PK `(/userId, /conversationId)` is the more future-proof choice and keeps a user's data co-located. `/userId` alone risks a hot/large partition for a heavy user (20 GB logical partition limit) — unlikely at POC scale, but the hierarchical key costs nothing to adopt now and is painful to change later.
- **`userId` = the Entra `oid` claim** (immutable), not UPN or email (both mutable).
- **Serverless** capacity mode for a POC. Provisioned/autoscale only if this goes real.
- **TTL** on messages if you want automatic retention — set it deliberately, since it's also your only "delete my data" story right now.
- **Storing per-message token counts is your real per-user usage ledger.** APIM enforces limits; Cosmos gives you the queryable history — per user, per model, over time — that a dashboard actually needs, and it survives APIM counter resets.
- **Context-window management** belongs here too: you can't send unbounded history to the model. Decide the policy — last N turns, token-budgeted window, or rolling summarization — and note that N differs per model since context windows differ (§3).
- **Cross-model conversations:** if a user switches from GPT-4o to Claude mid-thread, the prior turns go to a different model with a different tokenizer and different system-prompt semantics. Decide: allow it (store `model` per message, which the schema above does), or pin a conversation to a model. Allowing it is more useful and is a genuinely nice demo — "ask the same follow-up of three models" — but it interacts badly with prompt caching (new model = cold prefix).

---

## 8. Hosting — Container Apps alone vs. Static Web Apps + Container Apps

### Option 1 — ACA only (one or two container apps)

Serve the built React bundle as static files from the same FastAPI container (or a second container app behind the same environment).

- **Same origin** for UI and API → **no CORS**, no preflight, cookies simple. This removes a whole category of fiddly config, especially with SSE.
- One resource, one deploy pipeline, one place for logs and env config.
- Scale-to-zero is available, but with a chat app it means **cold starts on the first request after idle** — bad UX for a demo you open once a day. Set min replicas to 1 and accept the small always-on cost, or accept the cold start.
- Static assets are served by your Python process (or an nginx sidecar) rather than a CDN — irrelevant at POC scale.
- FastAPI needs correct ACA ingress config for SSE (no response buffering, adequate idle timeout for long generations).

### Option 2 — SWA (frontend) + ACA (API)

- SWA gives global CDN distribution, free/cheap hosting, automatic PR preview environments, and built-in auth. **Linked backends** let SWA proxy `/api/*` to your Container App, which restores same-origin behavior and forwards auth context.
- More moving parts, two deploy pipelines, and the SWA↔ACA link is another thing to get right.
- SWA's built-in Entra auth (EasyAuth-style) is convenient but constrained; for an SPA calling an API you'll likely want **MSAL.js** in the React app anyway, holding an access token for your FastAPI audience. Mixing SWA built-in auth with MSAL is a known source of confusion — pick one.
- Verify SWA linked-backend behavior with **SSE/streaming** before committing; proxy layers are where streaming quietly breaks.

### Recommendation

**Option 1 (ACA only) for this POC.** Your users are a small internal set, so CDN edge distribution buys you nothing, while same-origin simplicity and a single deployment unit buy you real time — and the streaming path has fewer proxies to misbehave. Option 2 is the right answer when the frontend is public, global, and iterating independently. Keep the React build output decoupled (it's just static files) so moving to SWA later is a deployment change, not a rewrite.

---

## 9. What I think you're missing

Ordered roughly by how much pain each one causes if ignored.

1. **A token is not a unit of cost.** Opus-4.5 tokens are dramatically more expensive than GPT-4o tokens, which are more expensive than a self-hosted Qwen token. A single `token-quota` per user treats them identically, so a user can burn your budget on Opus while appearing well within quota. **Consider a separate `llm-token-limit` per model** (the policy can be used multiple times, and counter keys can be composed, e.g. `@(userId + ":" + model)`), or a cost-weighted ledger computed in the BFF from the per-message data in Cosmos. This is the single biggest gap in the design as stated.
2. **Nothing in the design tells the user where they stand.** The policy can emit `remaining-tokens-header-name` / `remaining-quota-tokens-header-name`; surface those to the UI. Otherwise the first sign of a limit is an opaque failure.
3. **429 vs 403 are different failures.** Rate limit exceeded → **429** (retry shortly, honor `Retry-After`). Quota exhausted → **403** (retrying won't help until the period resets). The UI must say different things. Easy to conflate; confusing when you do.
4. **Content safety.** No moderation in the design. `llm-content-safety` with Azure AI Content Safety and prompt shields — and it pairs specifically with semantic caching, where a poisoned cache entry can be served repeatedly.
5. **Observability.** `llm-emit-token-metric` → App Insights with dimensions for model, user, and product. ⚠️ Watch **metric dimension cardinality**: user ID as a dimension is fine for a small internal group and expensive/limited at scale. Also confirm dimension prerequisites in the App Insights integration.
6. **Keyless auth to Foundry.** APIM → Foundry with **managed identity** (Azure AI User / Cognitive Services OpenAI User role) instead of API keys. Supported natively in the unified model API's backend config. No model keys anywhere.
7. **Token counting is per-gateway, not aggregated across the instance** — fine for a single-region POC, but it silently under-enforces if you ever add regions or workspaces. Note it now.
8. **Concurrency overshoot.** Documented: because actual token counts aren't known until the response returns, concurrent requests can temporarily exceed the limit. Limits are approximate at the edges by design. Don't build anything that assumes hard precision.
9. **A "stop generating" button** — expected in any chat UI in 2026, and it needs cancellation plumbing (§5.6).
10. **Conversation lifecycle**: rename, delete, list, search. Trivially easy to design in now; annoying to bolt on later.
11. **Deployment/IaC and secrets.** Bicep or Terraform for APIM (policies especially — hand-editing policy XML in the portal is not reproducible), Foundry, Cosmos, Redis, ACA. Managed identity from ACA → Cosmos and → Key Vault. No connection strings in app settings.
12. **There is a `.env` in this repo already.** Confirm it contains no live secrets before this directory becomes a git repository, and add `.gitignore` from the start.
13. **Cost of the supporting cast.** Azure Managed Redis for semantic caching and APIM v2 are not trivial line items — for a small POC they may well exceed your model spend. Worth pricing before committing, and worth knowing whether semantic caching earns its keep at your volume (at low traffic with per-user `vary-by`, it may not).
14. **Evaluation.** With three models selectable, "which is actually better for our prompts" is the interesting question. Foundry has evaluation tooling; logging per-message model + latency + tokens (§7) is the prerequisite, and you're already capturing it.

---

## 10. Proposed build order

Each phase ends in something demonstrable.

- **Phase 0 — Spike the risky unknowns.** Verify model availability/quota/region for all three; deployment type per model; whether the unified model API handles your three backends; whether `cache_control` survives translation; SSE end-to-end through APIM. *Do this before writing application code* — every one of these can change the architecture.
- **Phase 1 — Foundations.** Resource group, Foundry project + three deployments, APIM v2, Cosmos, App Insights. IaC from the first commit.
- **Phase 2 — Thin vertical slice.** FastAPI + one model, non-streaming, no auth, no cache. Prove the call path.
- **Phase 3 — APIM in the path.** Unified model API with all three models + aliases; managed identity to Foundry; model selector working end to end.
- **Phase 4 — Identity.** Entra app registrations (SPA + API), MSAL in React, JWT validation in FastAPI, `oid` as the user key.
- **Phase 5 — Persistence.** Cosmos schema, history rehydration server-side, conversation list/rename/delete.
- **Phase 6 — Metering.** `llm-token-limit` + `llm-emit-token-metric`, per-model counter keys, remaining-token headers surfaced in the UI, 429/403 handled distinctly.
- **Phase 7 — Streaming.** SSE end to end, stop button, exact usage from the final chunk, partial-message persistence.
- **Phase 8 — Caching.** Redis + embeddings backend, semantic cache with conservative `score-threshold` and `vary-by`; prompt-cache-friendly prompt assembly; measure hit rate and decide if it earns its cost.
- **Phase 9 — Safety & polish.** Content safety, dashboards, cost-weighted usage reporting.

---

## 11. Open questions for you

1. **Per-user APIM subscriptions**: hard requirement, or a means to per-user limits? (§4) — biggest open decision.
2. **Unified model API (preview)** vs. **your own adapters in FastAPI**? (§2)
3. **APIM SKU**: Basic v2 or Standard v2? (§2)
4. **Semantic cache partitioning**: per-user (safe, low hit rate) or per-group/model (useful, accepts cross-user answer reuse)? (§6a)
5. **Model switching mid-conversation**: allowed, or pinned per conversation? (§7)
6. **Quotas**: what actual numbers, and per-model or aggregate? (§9.1)
7. **Retention**: how long do conversations live? Any deletion requirement?
8. **Budget ceiling for the POC** — this determines whether Redis/semantic caching is in scope at all.

---

## Sources

Grounded against current Microsoft Learn documentation:

- [AI gateway capabilities in Azure API Management](https://learn.microsoft.com/en-us/azure/api-management/genai-gateway-capabilities)
- [Create and manage a unified model API (preview)](https://learn.microsoft.com/en-us/azure/api-management/unified-model-api)
- [`llm-token-limit` policy reference](https://learn.microsoft.com/en-us/azure/api-management/llm-token-limit-policy)
- [`llm-semantic-cache-lookup` policy reference](https://learn.microsoft.com/en-us/azure/api-management/llm-semantic-cache-lookup-policy)
