# Multi-LLM Chatbot on Microsoft Foundry — Design Notes

> **Historical design, superseded for implementation and deployment.**
> Current hosting is independent React on SWA plus FastAPI on ACA, direct
> cross-origin streaming, tenant users with delegated permission, and optional
> Cosmos history. Use [the root README](../README.md),
> [split deployment](split-deployment.md), and [API contract](api-contract.md).
> Older ACA-only, owner-only and resource-mutation decisions below are not the
> current instructions; shared Foundry remains read-only.

**Status:** design discussion, revision 3. Nothing implemented yet.
**Owner:** Ryan Harrington
**Subscription:** `<your-subscription>` — resolve with `az account show --query id -o tsv`
**Companion doc:** [`deployment-guide.md`](deployment-guide.md) — ordered manual build steps.
**Rev 3 changes:** capacity decision locked (§1) · corrected the "capacity is free so raise it" claim (§1a, §3) · single-user implications applied to concurrency (§6), the subscription-vs-counter-key question (§4), and eval scheduling (§1b) · Cost Management budget added to Phase 1.
**Rev 3.1:** target RG `multi-llm-chatbot-rg`; reusing Foundry project `ai-chatbot-multi-llm` on `cog-hao-dev-t7h`; **manual deployment first, Terraform deferred**; evaluated and rejected the new APIM **AI Gateway tier** (see deployment guide, Stage 1 aside).

---

## 0. Decisions locked (rev 2)
| Area | Decision |
|---|---|
| Model routing | **APIM unified model API** (preview), not hand-rolled FastAPI adapters |
| APIM SKU | **Basic v2** |
| Models | **gpt-4o**, **gpt-5.6-luna**, **DeepSeek-V4-Flash** (Anthropic unavailable in this subscription) |
| Call path | **BFF** — React → FastAPI on ACA → APIM → Foundry |
| Hosting | **ACA only** (container count discussed in §8) |
| Streaming | **Yes, from the start** |
| Quota UX | **Yes** — surface remaining tokens to the user |
| Cancel button | **Yes** — cancel in-flight generation |
| Observability | **Application Insights** |
| Auth to Foundry | **Keyless** (managed identity) |
| Content safety | **Deferred** — layer in later |
| Semantic caching | **See §10** — cost objection resolved, decision revised |
| IaC | **Terraform**, all resources |
| Evaluation | **Full LLMOps pipeline** — new §12 |
| Networking | Public internet |
| Multi-tenancy | Out of scope |
| **Users** | **Single user (owner) — sole subscription user and sole tester for the near term** |
| **Deployment capacity** | **luna → 10, DeepSeek-V4-Flash → 10, gpt-4o unchanged at 150** |
| **Cost-control posture** | **Deliberately low TPM caps as a spend-rate ceiling** (§1a) |

**Still open:** per-user APIM subscriptions vs. counter-key (§4, now a preference not an architecture question) · per-model vs. aggregate quotas (§9.1) · retention · budget ceiling.

---

## 1. Model inventory and capacity decision

I queried your subscription. All three models are on **one** account, which is good news:

**`cog-hao-dev-t7h`** · resource group `health-agent-orchestrator-rg` · **swedencentral** · kind `AIServices`

| Model | Version | SKU | Capacity (was → now) | Effective limits | Regional cap |
|---|---|---|---|---|---|
| gpt-4o | 2024-08-06 | GlobalStandard | 150 → **150** (unchanged) | 150,000 TPM / 1,500 RPM | 1,350 (600 used) |
| gpt-5.6-luna | 2026-07-09 | GlobalStandard | 1 → **10** | 10,000 TPM / 10 RPM | 1,000 |
| DeepSeek-V4-Flash | 2026-04-23 | GlobalStandard | 9 → **10** | 10,000 TPM / 10 RPM | 250 |

⚠️ **RPM-per-unit differs by model family.** gpt-4o grants **10 RPM per capacity unit**; luna and DeepSeek grant **1 RPM per unit**. So capacity 10 means 1,500 RPM on gpt-4o but only 10 RPM on the other two. Don't assume the ratio is uniform when sizing.

⚠️ **Quota is subscription-wide per region, not per account.** gpt-4o already shows 600 of 1,350 consumed in swedencentral by other work. Another project can erode your headroom without touching your account.

### 1a. Why capacity is deliberately low — and a correction

An earlier revision of this document argued that since capacity is free, there was "no financial reason" to leave it low. **That was wrong, and the reasoning is worth recording** because it affects how the whole cost story fits together.

Both statements are true, but they are about different things:

- **You are not billed for provisioned capacity on Global Standard.** Idle deployments cost $0.00 (§3). That part stands.
- **But capacity is a spend-*rate* ceiling.** On a pay-per-token service, the maximum tokens per minute *is* the maximum dollars per minute. That makes it a legitimate cost control, and the argument "raising it costs nothing" quietly ignores it.

For a solo developer the realistic risk isn't concurrent users — it's a **retry storm, a runaway loop in dev code, or a test harness that fires 500 requests unattended**. A low TPM cap bounds that blast radius. **Capacity 10 is a deliberate choice, not an oversight.**

**The important limitation:** capacity caps *rate*, not *total*. 10,000 TPM sustained is 600,000 tokens/hour and 14.4M tokens/day. Multiply by your model's output rate and an unattended overnight loop is still a real number. So capacity is the **throttle, not the fuse**. Pair it with:

1. **Azure Cost Management budget + alert** on the subscription — the only true spend ceiling.
2. **APIM `token-quota` with a `Daily` period** — a *total* cap, which is precisely what deployment capacity cannot express.

Which produces a pleasing property: **the cost control you need and the metering feature you're building are the same thing.** Deployment capacity is the backstop for when APIM is bypassed, misconfigured, or called directly during development.

### 1b. Where capacity 10 will actually bite: evaluation runs

With a single user, 10,000 TPM / 10 RPM is comfortable for interactive testing. One place it isn't:

**Eval runs (§12) draw on the same quota as live traffic.** A 50-prompt golden set at 10 RPM takes ~5 minutes per model — ~15 minutes across all three — and monopolizes the quota throughout. Two mitigations:

- **Bump capacity for the run, drop it after.** Capacity changes are free and effectively instant. Script this into the eval pipeline rather than relying on memory.
- **Or use a separate deployment for evaluation**, so eval and interactive traffic never contend.

### 1c. Second observation — the shared resource group

These deployments live in `health-agent-orchestrator-rg`, which is another workload's resource group. Two consequences worth deciding on:

1. You share **account-level quota** with that project. Its traffic can starve yours.
2. Terraform (§11) should **reference, not manage** that account, or you risk your `terraform destroy` taking out someone else's orchestrator.

Consider a dedicated AIServices account for this app. It costs nothing to create — S0 accounts have no fixed fee.

---

## 2. "The three models don't share a wire protocol" — what I meant, and why it no longer applies

### What a wire protocol is here

Two models can both "take a prompt and return text" and still be incompatible at the HTTP level. The wire protocol is the literal contract: URL shape, auth header, JSON field names and nesting, streaming event format, error envelope. Swapping one for the other means rewriting the request and re-parsing the response, not changing a string.

Concretely, this is what OpenAI vs. Anthropic looked like:

| | OpenAI Chat Completions | Anthropic Messages |
|---|---|---|
| System prompt | a message with `role: "system"` inside the array | a **top-level `system` field**, outside the array |
| Token cap | `max_tokens`, optional | `max_tokens`, **required** |
| Content | `content` is a plain string | `content` is an **array of typed blocks** |
| Response text | `choices[0].message.content` | `content[0].text` |
| Streaming | `data:` chunks with `choices[0].delta` | **named events** — `message_start`, `content_block_delta`, `message_stop` |
| Auth | `Authorization: Bearer` / `api-key` | `x-api-key` + `anthropic-version` |
| Stop reason | `finish_reason` | `stop_reason`, different vocabulary |

Two things that both "do chat" but share almost no field names. That is a wire-protocol difference, and it is the problem the unified model API exists to solve — it translates between these two shapes so your client only ever writes the OpenAI form.

### Your instinct was right, and your new model list dissolves the problem

You read the docs correctly: OpenAI + Anthropic is *the* common pairing, because those are exactly the two formats the unified model API translates between. And the difficulty was never Qwen specifically — it was **Anthropic**, which was the one genuinely foreign protocol in your original three.

With Anthropic out, all three of your models speak the **OpenAI Chat Completions** shape:

- **gpt-4o** — Azure OpenAI, native.
- **gpt-5.6-luna** — Azure OpenAI, native.
- **DeepSeek-V4-Flash** — a Foundry model, exposed OpenAI-compatible.

**So I should be straight with you: the strongest argument I made for the unified model API is now gone.** The remaining differences are much smaller than a protocol difference:

- **Routing/path shape.** AOAI models are addressed as `/openai/deployments/{deployment}/chat/completions` (model identified by *URL path*); Foundry models are addressed as `/models/chat/completions` with the model in the *request body*. Real, but it's a routing concern, not a translation concern.
- **Parameter quirks.** Newer reasoning-capable models often require `max_completion_tokens` instead of `max_tokens` and restrict `temperature`. Verify per model in Phase 0 — this is now your most likely source of "works for gpt-4o, 400s for luna."

### Does that change the decision?

**No — I still land on the unified model API**, but for weaker and more honest reasons than I originally gave:

1. **One endpoint, one auth, one policy set** across both the AOAI-style and Foundry-style path shapes.
2. **Aliases decouple client from deployment.** Your React dropdown sends `"fast"` / `"balanced"` / `"frontier"`; you re-point an alias to a different backend without touching app code. For a project whose model list has already changed once, that is worth real money.
3. **Policies attach once** rather than per-API.
4. **You keep the option** to add a genuinely foreign backend later without a code change.

The cost is that it's **preview**. Since preview + `1 RPM` are both Phase 0 risks, validate them together. If the unified API misbehaves, your fallback is now cheap precisely *because* all three are OpenAI-shaped — a plain APIM API with routing rules would do.

### And Basic v2?

Worth naming: my SKU recommendation was driven by "Anthropic policies need v2." That driver is gone. **Basic v2 is still the right choice** — the unified model API supports it, it's the cheapest v2, and v2 is the direction of the platform — but it's now a preference rather than a constraint. At **~$150/month** (`$0.20548/hr`, verified via retail pricing) versus Standard v2 at ~$700/month, Basic v2 is clearly right for a POC. It will be your **largest fixed cost by an order of magnitude**.

---

## 3. ✅ Cost confirmation — idle deployments cost you nothing

**Confirmed for all three of your models.** All are `GlobalStandard`, and per the Foundry deployment-types documentation, Global Standard is **pay-per-token**. Provisioned (`GlobalProvisionedManaged`, `ProvisionedManaged`) reserves capacity and bills whether or not you use it; **Standard and Global Standard do not.** A deployed-but-unused Global Standard model bills **$0.00**.

Specifically:

- **gpt-4o (GlobalStandard, 150)** — no idle cost. ✅
- **gpt-5.6-luna (GlobalStandard, 10)** — no idle cost. ✅
- **DeepSeek-V4-Flash (GlobalStandard, 10)** — no idle cost. ✅

**The key mental model:** the capacity number is **quota, not a reservation.** It's a ceiling the service enforces on you, not throughput you've bought. Raising capacity does not change your bill by a cent — it changes the rate at which you're *allowed* to spend.

⚠️ **Don't over-read that.** "Free to raise" is not the same as "no reason not to raise." Because capacity bounds tokens-per-minute, it also bounds **dollars-per-minute** — which makes a low cap a real guardrail against runaway spend, and is exactly why luna and DeepSeek are deliberately held at 10. See §1a.

Also confirmed:

- The `S0` Cognitive Services / AIServices account itself has **no fixed monthly fee**.
- Deleting a Global Standard deployment saves nothing, because it was already free.

**Three caveats:**

1. **This holds for Global Standard specifically.** If you ever deploy a model on **managed compute** (some open-weight catalog models only offer this), you rent GPU hours and it bills **continuously whether or not anyone chats**. That is the classic surprise bill in Foundry. All three of yours are serverless, so you're clear — just re-check before adding a fourth model.
2. **Some partner-published catalog models bill through Azure Marketplace** with separate terms. Yours are sold directly by Azure.
3. **Per-token prices differ enormously between these three models.** This is exactly why a flat per-user token quota is the wrong instrument (§9.1) — it's the one cost topic that actually matters here, and it's still open.

---

## 4. Call path — BFF confirmed

```
React SPA
   │  Entra ID access token (MSAL.js)
   ▼
FastAPI on Azure Container Apps          ← the BFF
   │  validates JWT · extracts oid · allowlists model alias
   │  rehydrates history from Cosmos (never trusts client history)
   │  holds the APIM subscription key
   ▼
APIM Basic v2 — unified model API
   │  llm-token-limit (per user, per model)
   │  llm-emit-token-metric → App Insights
   │  managed identity → Foundry
   ▼
cog-hao-dev-t7h (swedencentral) — gpt-4o · gpt-5.6-luna · DeepSeek-V4-Flash
```

**Region note:** your models are in **swedencentral**. Put APIM, ACA, and Cosmos in the same region unless you have a reason not to — a US-region APIM calling a Sweden endpoint adds a round trip on every token-generating call, and on a streaming path that latency shows up in TTFT.

**Still open — but the nature of the question has changed:** per-user APIM subscriptions vs. a `counter-key` over the validated `oid` claim.

⚠️ **With exactly one user, this is no longer an operational tradeoff.** My rev 1 argument against per-user subscriptions was provisioning overhead, secret sprawl, and deprovisioning — all of which are meaningless for a population of one. So the decision collapses to: **which pattern do you want to build and demonstrate?**

- **Counter-key** — less code, no provisioning path, no keys to store. Demonstrates claim-based metering.
- **Per-user subscriptions** — exercises the APIM products/subscriptions model, per-user tiers, and per-user key revocation. More machinery, but it's the machinery you'd actually need if this ever grew past you, and building it now with one user is the cheapest time to learn it.

Since this is a POC whose purpose is partly to prove the pattern, **the "more realistic" option is now also the low-risk one.** Worth deciding on that basis rather than on effort.

---

## 5. Deep dive — "token counting is per-gateway, not aggregated across the instance"

### The mechanism

An APIM "instance" is not one server. It's a logical resource that fronts a set of **gateways**, and a gateway is itself a set of compute nodes. When `llm-token-limit` enforces "50,000 tokens per user per day," something must hold the running count. There is no central transactional counter service behind APIM — **the count lives in the gateway.**

That produces two distinct levels of imprecision, and they're often conflated.

**Level 1 — across gateways: no aggregation at all.**

The documentation is explicit: the policy *"tracks token usage independently at each gateway where it is applied, including workspace gateways and regional gateways in a multi-region deployment. It doesn't aggregate token counts across the entire instance."*

Each gateway keeps its **own private counter** for the same `counter-key`. They do not talk to each other. So the real enforced limit is:

> **effective limit ≈ configured limit × number of gateways**

Add a second region for latency and every user's quota silently doubles. Nothing errors; nothing warns. Your 50,000/day becomes 100,000/day. The same applies to workspace gateways and self-hosted gateways.

**Level 2 — within one gateway: propagated, but eventually consistent.**

Inside a single gateway, nodes *do* share counter state, but asynchronously. Rate-limit state is propagated between nodes quickly; quota state is propagated more slowly because it's a longer-horizon measure. The docs carry a blunt caution: *"Because of the distributed nature of throttling architecture, rate limiting is never completely accurate."* And separately, that after a platform restart APIM *"might continue to handle requests for a short period after a quota is reached."*

So even one gateway gives you an approximation — a good one, but an approximation.

**Level 3 — v2 tiers change the algorithm.** Basic v2 uses a **token bucket**, not the sliding window used by classic tiers. The bucket starts full at `tokens-per-minute`, so **you get an initial burst of the full limit**, then refill at limit/60 per second. A user idle for ten minutes can spend their entire per-minute allowance instantly. That's usually desirable for chat (it absorbs a bursty first message) but it is not "smooth per-minute pacing," and it's worth knowing before you debug why someone exceeded a per-minute cap in three seconds.

⚠️ Also v2-specific: if you configure the same `counter-key` at multiple scopes, **all instances must use identical `tokens-per-minute` and renewal values**, or behavior is undefined. Relevant to you, since §9.1 proposes per-model counters — keep the key composition and the limits consistent.

### What it means for you, concretely

**For this POC: essentially nothing.** You're deploying a **single-region Basic v2 instance, no workspaces, no self-hosted gateways** — that's exactly one gateway, one counting domain. You get Level 2 fuzziness only.

**Where it would bite you:**

- Adding a region for latency → all quotas silently double.
- Believing the quota is a hard financial control. It is a **guardrail against runaway usage, not a billing cap.** If you need a true spend ceiling, that belongs in Azure Cost Management budgets and alerts, plus the per-message ledger you're writing to Cosmos.
- Treating the number in the UI as exact. Which leads directly to the next section.

---

## 6. Deep dive — concurrency overshoot

### Why it happens

The policy cannot know a request's true cost until the response comes back. So with `estimate-prompt-tokens="false"`, each request follows this sequence:

1. **Inbound:** read the counter. Under the limit? Admit.
2. **Backend:** call the model. *Now* the tokens are actually spent.
3. **Outbound:** read `usage` from the response, add it to the counter.

The gap between steps 1 and 3 is the whole generation time — **seconds**. Every request that arrives inside that window sees a counter that has not yet been charged for any in-flight request.

Worked example. User has 1,000 tokens left. Eight browser tabs fire at once:

| | |
|---|---|
| t=0.00s | All 8 requests hit inbound. Counter reads 0/1000 for all 8. All 8 admitted. |
| t=0.0–4s | All 8 generate. Each costs ~800 tokens. |
| t=4s | Outbound charges 8 × 800 = **6,400 tokens** against a 1,000 limit. |
| t=4s+ | The 9th request is blocked. The horse has left. |

### How much this matters for you: much less, because you're a single user

⚠️ **Scope correction.** Overshoot is bounded by `concurrent_requests × max_tokens_per_request`. With **one user running one chat UI**, concurrency is effectively 1 — so worst-case overshoot is roughly *one request's* worth, not eight. The scenario above is what happens with a user population; it is not your day-to-day.

It still isn't zero, because concurrency > 1 can arise without a second person:

- Two browser tabs open on the same conversation.
- A retry (manual or automatic) fired before the first response lands.
- An **eval run overlapping interactive use** (§1b) — your most likely real trigger.

So the mitigations below still belong in the build, but treat them as **housekeeping rather than urgent risk mitigation** at this stage.

### Three things that shape the risk in your design

1. **Streaming.** Under `stream: true`, APIM **always estimates prompt tokens regardless of your setting, and estimates completion tokens too.** You've chosen streaming from the start, so your enforcement path is estimate-based by construction. Overshoot is inaccuracy stacked on inaccuracy.
2. **Your quotas are deliberately small.** At **10,000 TPM** on luna and DeepSeek, a handful of concurrent mid-conversation turns (~3,000 tokens each) consumes the minute. That's an accepted consequence of the §1a cost posture, not a defect — but it means overshoot shows up as visible 429s sooner than it would at 150,000 TPM.
3. **Foundry enforces its own limit independently.** APIM overshooting means requests reach Foundry that Foundry then rejects with its own 429. Your user sees a failure that your gateway thought it had prevented.

### Mitigations, in the order I'd apply them

1. **Cap `max_tokens` server-side in the BFF, on every request.** This is the single highest-leverage fix, because it converts unbounded overshoot into a computable worst case. Never let the client set it unbounded. It is also a direct spend control, which aligns with §1a.
2. **Limit concurrency per user in the BFF.** A chat UI needs at most one in-flight generation per conversation. An `asyncio.Semaphore(1)` keyed on user (or conversation) collapses the entire scenario above. You need per-user request tracking anyway for the cancel button, so this is nearly free.
3. **Add `rate-limit-by-key` alongside `llm-token-limit`.** Requests-per-minute is known *at admission time* with no estimation, so it's exact where token limits are not. At 10 RPM on two of your three models, this is worth having.
4. **Set APIM limits below the Foundry deployment quota.** Leave headroom for overshoot so APIM fails first, with a clean 429 you control, rather than leaking an opaque upstream error. At 10,000 TPM there is now room to do this — set the APIM per-minute limit to roughly 7,000–8,000.
5. **Consider `estimate-prompt-tokens="true"`.** Reserves the prompt side at admission, which shrinks the window. Costs some latency, and does nothing under streaming.
6. **Treat the remaining-token figure in the UI as approximate.** The docs say the estimate *"may be larger than expected... and becomes more accurate as the quota is approached."* Which brings us to §7.

---

## 7. Quota UX — telling the user where they stand

Confirmed in scope. Mechanism:

- `llm-token-limit` emits `remaining-tokens-header-name` (per-minute rate) and `remaining-quota-tokens-header-name` (longer-period quota).
- APIM returns them → the BFF reads them off the APIM response → forwards to the client.
- **Under streaming, headers arrive with the response *start*,** so they reflect state *before* this generation is charged. Update the display again after the final usage chunk (§13) for an accurate post-call figure.

**Design guidance:**

- Show the **quota** (e.g. "42,000 of 50,000 tokens left today"), not the per-minute rate. The rate limit is a transient hiccup; the quota is the thing a user needs to budget against.
- **Label it as approximate.** It genuinely is (§5, §6). A precise-looking number that drifts erodes trust more than an honest "~42,000."
- Distinguish the two failures clearly:
  - **429** — rate limit. *"Too fast — try again in a few seconds."* Honor `Retry-After`.
  - **403** — quota exhausted. *"You've used your allowance for today; it resets at midnight UTC."* Retrying will not help.
  Conflating these produces the worst possible UX: a retry button that can never succeed.
- **Per-model quotas (§9.1) mean per-model remaining counts.** The dropdown is the natural place — show the balance next to each model, which also nudges users toward the cheap one.

---

## 8. ACA — one container or two?

### Option A — single container (recommended)

Multi-stage Dockerfile: `node` builds the React bundle, then the bundle is copied into the Python image and FastAPI serves it via `StaticFiles` with a catch-all route returning `index.html` for client-side routing.

**For:**
- **Same origin. No CORS at all** — and CORS on an SSE endpoint is a genuinely annoying failure mode, because a misconfigured preflight fails in ways that look like a streaming bug.
- **No version skew.** The UI and API ship as one artifact. This matters more than it sounds for you: you're building a **custom SSE event contract** (`delta` / `error` / `usage` / `done`). If the frontend and backend can deploy independently, they can disagree about that contract in production. One image makes that structurally impossible.
- One deploy, one revision history, one log stream, one min-replica to pay for.

**Against:**
- **Coupled deploys.** A CSS change rebuilds and redeploys the API. On ACA that means a new revision, which means **in-flight SSE streams get dropped**. Mitigate with a `terminationGracePeriodSeconds` long enough to drain a typical generation, and deploy when nobody's mid-conversation. At POC scale this is a non-issue; at scale it argues for splitting.
- Frontend iteration requires the whole toolchain and a slower build.
- FastAPI serving static assets is slightly wasteful. Irrelevant at your volume.

### Option B — two container apps in one ACA environment

React on nginx, FastAPI separately.

**For:** independent deploy and scale cadence; nginx serves static properly; cleaner separation if the frontend gets its own owner.

**Against — and this is the part that's easy to underestimate:** two container apps get **two different FQDNs**. ACA has no built-in path-based routing *across* apps, so you cannot simply map `/api/*` to one and `/*` to the other. Your options are:

1. **Accept two origins and configure CORS** — including preflight on the streaming endpoint, and credential handling for the Entra token.
2. **Put Front Door or Application Gateway in front** for path routing — which adds a resource, cost, and *another* proxy that can buffer your SSE stream.
3. **Add an nginx reverse-proxy container** — at which point you have three containers to avoid the coupling of one.

Every path reintroduces complexity that Option A simply doesn't have.

### Recommendation

**Single container.** Your team is one person, your users are a small internal group, and your frontend and backend share a hand-rolled streaming protocol that benefits from shipping atomically. Split only when frontend deploy cadence becomes a real irritant — and by then you'll have Front Door or a proxy justified on other grounds.

**Either way, get these ACA settings right:**
- **`minReplicas: 1`.** Scale-to-zero means a cold start on the first message of the day — brutal on a chat demo.
- **Disable response buffering; raise the request idle timeout.** A long generation must not be cut off mid-stream.
- **HTTP ingress, external, port for uvicorn.**
- **`terminationGracePeriodSeconds`** long enough to drain active streams on revision change.
- **System-assigned managed identity**, for Cosmos and Key Vault (§11).

---

## 9. Metering, safety, observability

### 9.1 A token is not a unit of cost *(still open — the most important open question)*

Unchanged from rev 1, and **sharper now**: gpt-4o, gpt-5.6-luna, and DeepSeek-V4-Flash have very different per-token prices. A single `token-quota` per user treats them as interchangeable, so a user can drain the budget on the expensive model while appearing comfortably in quota.

Two approaches:

- **Per-model counters in APIM.** `llm-token-limit` may be used multiple times, and `counter-key` accepts an expression — compose it as `oid + ":" + model`. Simple, enforceable at the gateway, and it makes the per-model UI in §7 natural. ⚠️ Remember the v2 constraint: consistent values per counter key across scopes (§5).
- **Cost-weighted ledger in the BFF.** Multiply each model's tokens by its price and enforce a **dollar** budget from the Cosmos per-message data. Truer to intent, more code, and enforced after the fact rather than at the gateway.

**My lean: both.** Per-model APIM counters as the real-time guardrail; the Cosmos ledger as the reporting and analysis truth. The ledger is something you're building anyway for §12.

### 9.2 Confirmed in scope

- **App Insights** via `llm-emit-token-metric`, with dimensions for model, user, and product. ⚠️ Watch **dimension cardinality** — user ID as a metric dimension is fine for a small internal group, and gets expensive/limited at scale. Prefer user-level detail in Cosmos, aggregate-level in metrics.
- **Keyless auth to Foundry.** APIM system-assigned managed identity + **Cognitive Services OpenAI User** on `cog-hao-dev-t7h`. Configurable directly in the unified model API's backend settings. Same pattern ACA → Cosmos.
- **Cancel button** — see §13.

### 9.3 Deferred

- **Content safety** — deliberately out for now. Note it pairs specifically with semantic caching (a cached unsafe response gets served repeatedly), so **if semantic caching lands first, revisit this**. You already have a ContentSafety resource (`cs-llmopsworkshop-dev`) in `rg-llmops-dev`.

---

## 10. Semantic caching — I was wrong about the cost

You pushed back on Azure Managed Redis being expensive. **You were right to, and my rev 1 framing was wrong.** I checked retail pricing rather than assuming:

| Option | RediSearch? | ~Cost/month | Verdict |
|---|---|---|---|
| **Azure Managed Redis B0** (Balanced) | ✅ yes | **~$12** (`$0.016/hr`) | **Cheapest workable option** |
| Azure Managed Redis B1 | ✅ yes | ~$23 | Next step up |
| Azure Cache for Redis Basic C0 | ❌ **no** | ~$16 | **Will not work** — see below |
| Self-hosted Redis in ACA/VM | ✅ if Redis 8 / redis-stack | ~$8–15 | Cheaper in theory, worse in practice — see below |
| No semantic cache | — | $0 | Viable; see recommendation |

**Why Azure Cache for Redis Basic is disqualified despite being cheap:** its Basic, Standard, and Premium tiers run **community Redis**, which does not include the search/vector module. Azure Managed Redis runs the **Redis Enterprise stack**, which does. `llm-semantic-cache-lookup` performs a vector similarity search, so it requires a **RediSearch-compatible** cache. Picking the cheap classic tier will fail at runtime, not at deploy time — an easy and frustrating trap.

**Why self-hosting is a false economy here:** APIM's external cache does accept **"Custom"** with any Redis-compatible connection string, so a `redis-stack` container is *technically* allowed. But APIM connects over **raw TCP with a connection string** — ACA's HTTP ingress won't carry it, so you need TCP ingress and TLS you terminate yourself, or a VM with a public endpoint you now have to patch and secure. You'd be exposing an unauthenticated-by-default data store to the internet to save roughly four dollars a month. Don't.

⚠️ **One real constraint on Azure Managed Redis:** APIM authenticates to it with a **connection string** — **Microsoft Entra authentication is not currently supported** for the APIM→Redis link. So you must enable access-key auth on the cache, and this becomes the **one place in the design where a key exists**. Keep it in Key Vault, surface it as an APIM named value, and note the exception explicitly rather than believing the system is fully keyless.

### Revised recommendation

**Provision it — it's $12/month — but keep it in Phase 8, and treat the hit rate as a hypothesis to be tested, not a benefit to be assumed.** The cost objection is dead; the *value* question is very much alive:

- With `vary-by` partitioning on **user + model** (required for correctness, §6a of rev 1), each user only ever hits their own cache entries, for one model.
- At small-internal-group volume, on **multi-turn** conversations, the hit rate may be near zero. Semantic caching shines on high-volume, one-shot, overlapping questions — a support FAQ bot, not a small exploratory multi-model chat.

So: build it, instrument hit rate as a first-class metric, and be willing to delete it. The `max-message-count` setting (skip caching past N messages) and a conservative `score-threshold` (start ~0.05; above 0.2 risks mismatches) remain as described in rev 1.

**You also need an embeddings deployment** for the cache — there isn't one on `cog-hao-dev-t7h` today. `text-embedding-3-small` is cheap and sufficient; you have one on other accounts already, but it must be reachable as an APIM backend with managed identity.

---

## 11. Terraform

**All resources in Terraform** — confirmed. Provider: `azurerm` (plus `azapi` for anything the main provider lags, which is common for preview features).

### Manage vs. reference

| Resource | Terraform stance |
|---|---|
| `cog-hao-dev-t7h` + its 3 deployments | ⚠️ **`data` source — reference only.** It lives in another project's RG (`health-agent-orchestrator-rg`). Managing it risks destroying someone else's workload. |
| New dedicated AIServices account (if you take the §1 advice) | `resource` — manage |
| Resource group (new, for this app) | `resource` |
| APIM Basic v2 + unified model API + policies + backends + products | `resource` |
| Cosmos DB (serverless) + containers | `resource` |
| ACA environment + container app + ACR | `resource` |
| Log Analytics + Application Insights | `resource` |
| Key Vault + secrets | `resource` (values via variables, never literals) |
| Role assignments (APIM→Foundry, ACA→Cosmos, ACA→KV) | `resource` |
| Azure Managed Redis B0 (Phase 8) | `resource` |
| Entra app registrations | `azuread` provider, or click-ops and reference by ID — your call |

### Things that will bite you

1. **APIM policies are XML.** Keep them as separate `.xml` files loaded via `file()`/`templatefile()`, not heredocs inside `.tf`. They're the highest-churn part of this system and you'll want them diffable and reviewable on their own.
2. **The unified model API is preview.** `azurerm` may not model it yet — expect `azapi_resource` for that piece, and pin API versions.
3. **APIM Basic v2 takes ~30–45 minutes to create.** Plan for it; don't destroy/recreate casually. Consider a separate state/stack for long-lived infrastructure vs. fast-moving app resources.
4. **Remote state** in a storage account with locking, from commit one.
5. **`.env` already exists in this directory.** Confirm it holds no live secrets and add `.gitignore` **before** `git init`. Terraform state also contains secrets — never commit it.

---

## 12. LLMOps and evaluation *(new)*

This is the section that turns "a chatbot with a dropdown" into something with a defensible answer to *"which model should we actually use?"* — and you already have `rg-llmops-dev` in the subscription to build on.

### 12.1 The layers

**1. Offline evaluation — before deploy, gates releases.**
A curated golden dataset of representative prompts (plus expected answers or grading criteria) run against each model on every meaningful change. Use the Foundry evaluation SDK (`azure-ai-evaluation`) with quality evaluators — groundedness, relevance, coherence, fluency, similarity, F1 — and later the risk/safety evaluators when content safety lands.

⚠️ **AI-assisted evaluators need a judge model deployment.** Budget for it: evaluation runs consume tokens, and a full sweep across three models with several evaluators is not free. Use gpt-4o as judge; note that judging with a model that is also under test introduces self-preference bias, so interpret head-to-head results with that caveat.

**2. Comparative evaluation — your project's signature artifact.**
You have three models behind stable aliases. Run the *same* dataset across all three and produce a matrix of **quality × latency × cost per prompt category**. That's the deliverable that justifies the whole build, and it converts §9.1's cost-weighting from an accounting chore into a decision tool: *DeepSeek is 94% as good as gpt-4o on our prompts at a fraction of the price, so it should be the default and gpt-4o the escalation.*

**3. Online evaluation — continuous, on production traffic.**
Sample real conversations, score them on a schedule, alert on regression. Model versions change under you; a model that was fine last month can drift.

**4. Human feedback.**
👍/👎 on each response, written to Cosmos. The cheapest, highest-signal thing on this list, and it's a small UI change — **add it to the frontend scope now**, because retrofitting feedback onto a message schema is annoying. Thumbs-down messages are your best source of new eval cases.

### 12.2 What the app must do to make this possible

The chatbot *is* the data collection pipeline. Requirements that must land in Phase 5, not bolted on later:

- **Rich per-message records in Cosmos** — `model`, `promptTokens`, `completionTokens`, `latencyMs`, `ttftMs`, `cacheHit`, `finishReason`, `promptVersion`, `feedback`.
- **Prompt versioning.** Treat the system prompt as a **versioned artifact in the repo**, not a string in app settings, and stamp `promptVersion` on every message. Without it you cannot attribute a quality change to a prompt change.
- **Correlation IDs** flowing SPA → BFF → APIM → Foundry, so an App Insights trace joins to a Cosmos conversation.
- **Tracing** via OpenTelemetry into App Insights; Foundry tracing for the model-side view.

### 12.3 Dataset curation loop

```
production conversations (Cosmos)
      │  sample — prioritize 👎 and high-token outliers
      ▼
  review / label
      │
      ▼
  golden dataset (versioned in git, or a Foundry evaluation dataset)
      │
      ▼
  offline eval across all 3 aliases ──► quality × latency × cost matrix
      │
      ▼
  CI gate: block merge on regression beyond threshold
```

### 12.4 CI/CD

- **PR pipeline:** lint, unit tests, `terraform plan`, build image.
- **Eval pipeline:** on changes to prompts, model aliases, or retrieval logic, run the golden set and **fail the build on regression beyond a threshold**. This is the single practice that separates LLMOps from "we tested it by hand."
- **Deploy:** push to ACR, `terraform apply`, ACA revision.
- **Post-deploy:** smoke test each of the three aliases end to end — you have three independent backends, and any one can break alone.

⚠️ Eval runs cost tokens against the *same* deployments the app uses, and share quota. At **10,000 TPM / 10 RPM**, a 50-prompt golden set takes ~5 minutes per model and monopolizes the quota while running — so an eval run and interactive use will contend. Either script a temporary capacity bump into the eval pipeline (free, instant, revert after — see §1b) or use a separate deployment for evaluation.

**Tooling note:** there's a `microsoft-foundry` skill available in this environment covering agent evaluation, batch/continuous eval, and CI/CD — worth invoking when you reach this phase.

---

## 13. Streaming and cancellation

Streaming confirmed from the start. Full mechanics in rev 1 §5; the additions:

### Cancel button

1. React holds the `AbortController` for the `fetch`; **Stop** calls `.abort()`.
2. That closes the HTTP connection to the BFF.
3. FastAPI detects the disconnect (`await request.is_disconnected()`, or the `CancelledError` raised in the generator) and **must propagate it** — closing the `httpx` stream to APIM, which closes to Foundry.
4. **If you skip step 3, you keep paying for tokens nobody will read.** The generation continues server-side; only the UI stopped listening. This is the entire point of wiring cancellation properly.
5. Persist the partial assistant message to Cosmos flagged `cancelled: true`, with tokens consumed up to that point — it's real spend and belongs in the ledger.

### SSE event contract

Define it explicitly now, since one container ships both halves (§8):

| Event | Payload | Notes |
|---|---|---|
| `delta` | `{ "text": "..." }` | append to the message |
| `usage` | `{ promptTokens, completionTokens, remainingQuota }` | final chunk; **exact** figures |
| `error` | `{ code, message, retryAfter? }` | in-band; a mid-stream 429/403 cannot be an HTTP status |
| `done` | `{ messageId }` | commit, stop the spinner |

Request `stream_options: {"include_usage": true}` so the final chunk carries exact usage — **enforcement stays estimate-based (§6), but your reporting and your Cosmos ledger become exact.** That split is the key idea: approximate at the gate, precise in the books.

---

## 14. Rough monthly cost (POC, low usage)

| Item | ~$/month | Note |
|---|---|---|
| **APIM Basic v2** | **~$150** | verified; dominant fixed cost |
| Azure Managed Redis B0 | ~$12 | Phase 8; skip until then |
| ACA (1 replica always on) | ~$15–30 | `minReplicas: 1` |
| Cosmos serverless | ~$1–5 | RU + storage, tiny at this scale |
| App Insights | ~$0–10 | ingestion-based; watch metric cardinality |
| Log Analytics | ~$0–5 | |
| ACR Basic | ~$5 | |
| Key Vault | <$1 | |
| **Model tokens** | usage-only | **$0 when idle** (§3) |
| **Foundry deployments idle** | **$0** | confirmed |
| **Total fixed** | **~$185–215** | ~80% of it is APIM |

If that ceiling is uncomfortable, the only meaningful lever is APIM — and removing APIM removes token limiting, semantic caching, and centralized metering, i.e. most of the project. Worth confirming the budget before Phase 1.

⚠️ **Note the shape of this budget.** Given the §1a cost posture, the risk is inverted from a typical AI project: **your fixed infrastructure cost dominates and your variable token cost is small**, because capacity caps hold token burn to a trickle. Optimizing prompts to save tokens is close to pointless here; the ~$150/month APIM instance is the number that matters.

**Action item (§1a):** set an **Azure Cost Management budget with alerts** on the subscription. Deployment capacity throttles the *rate*; the budget is the only thing that catches a sustained burn. Do this in Phase 1, not Phase 9.

---

## 15. Build order (revised)

- **Phase 0 — Spikes.** Confirm all three models answer an OpenAI-shaped request; check `max_tokens` vs `max_completion_tokens` and temperature constraints per model; stand up the unified model API and verify it fronts both AOAI-path and Foundry-path backends; prove SSE end to end through APIM.
- **Phase 1 — Terraform foundations.** RG, APIM Basic v2, Cosmos, ACA env, ACR, Log Analytics + App Insights, Key Vault, remote state, **Azure Cost Management budget + alerts (§14)**. Reference the existing Foundry account as a data source.
- **Phase 2 — Vertical slice.** FastAPI + gpt-4o, **non-streaming**, no auth, no cache. Prove the path.
- **Phase 3 — APIM in the path.** Unified model API, three aliases, managed identity to Foundry, dropdown working end to end.
- **Phase 4 — Identity.** Entra app registrations, MSAL.js, JWT validation, `oid` as user key.
- **Phase 5 — Persistence + telemetry schema.** Cosmos containers, server-side history rehydration, **the full per-message record from §12.2** (including `promptVersion` and a feedback field), conversation list/rename/delete.
- **Phase 6 — Metering.** Per-model `llm-token-limit`, `rate-limit-by-key`, `llm-emit-token-metric`, remaining-token headers in the UI, distinct 429/403 handling, BFF-side `max_tokens` cap and per-user concurrency limit (§6).
- **Phase 7 — Streaming + cancel.** SSE contract, abort propagation, exact usage from final chunk, partial-message persistence.
- **Phase 8 — Semantic caching.** AMR B0, embeddings deployment, conservative threshold, `vary-by` user+model, **measure hit rate and be willing to remove it**.
- **Phase 9 — LLMOps.** Golden dataset, comparative eval across all three, CI eval gate, tracing, feedback loop.
- **Phase 10 — Content safety and polish.** `llm-content-safety`, dashboards, cost-weighted reporting.

---

## 16. Open questions

1. ~~Will you raise gpt-5.6-luna's capacity?~~ ✅ **Resolved** — luna and DeepSeek to 10 (10,000 TPM / 10 RPM), gpt-4o unchanged at 150. Deliberate spend-rate ceiling (§1a).
2. **Dedicated Foundry account**, or keep using `cog-hao-dev-t7h` in another project's RG? (§1c)
3. **Per-user APIM subscriptions vs. counter-key?** (§4) — now a *preference* question, not an architecture one, since you're the only user. Which do you want to build and show?
4. **Per-model quotas — what actual numbers?** (§9.1) Note these must sit **below** 10,000 TPM to fail at APIM rather than Foundry; ~7,000–8,000 is a reasonable starting point.
5. **Region:** everything in **swedencentral** with the models? (§4)
6. **Retention** for conversations?
7. **Budget ceiling** — is ~$200/month fixed acceptable, and what number should the Cost Management alert fire at? (§14)
8. **Which pieces are already deployed** that you want reused? I found `rg-llmops-dev` (incl. a ContentSafety resource) and several embeddings deployments — tell me what's fair game.

---

## Sources

Verified against Microsoft Learn and the Azure retail pricing API (2026-09-01). Live subscription state read via Azure CLI.

- [AI gateway capabilities in Azure API Management](https://learn.microsoft.com/en-us/azure/api-management/genai-gateway-capabilities)
- [Create and manage a unified model API (preview)](https://learn.microsoft.com/en-us/azure/api-management/unified-model-api)
- [`llm-token-limit` policy reference](https://learn.microsoft.com/en-us/azure/api-management/llm-token-limit-policy)
- [`llm-semantic-cache-lookup` policy reference](https://learn.microsoft.com/en-us/azure/api-management/llm-semantic-cache-lookup-policy)
- [Advanced request throttling with Azure API Management](https://learn.microsoft.com/en-us/azure/api-management/api-management-sample-flexible-throttling)
- [Use an external cache in Azure API Management](https://learn.microsoft.com/en-us/azure/api-management/api-management-howto-cache-external)
- [Understanding deployment types in Microsoft Foundry Models](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types)
- [Azure Managed Redis architecture](https://learn.microsoft.com/en-us/azure/redis/architecture)
