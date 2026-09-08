# Streaming and operational guardrails

The [API contract](api-contract.md) defines browser-visible JSON, errors and SSE.
The BFF calls the existing APIM OpenAI-compatible API. APIM owns deployment
alias routing, provider parameter translation and downstream managed identity.
This document is a review checklist, **not authorization to change that gateway
or its shared Foundry backends**.

## Preserve streaming end to end

Review effective APIM policies at operation, API, product and global scope.
The effective backend `forward-request` must disable response buffering:

```xml
<backend>
  <forward-request timeout="180" buffer-response="false" />
</backend>
```

This is a fragment, not a replacement policy. Merge it deliberately with the
existing routing, authentication, token-limit and error policy. Do not introduce
duplicate/inherited forwarding or automatically overwrite the gateway. Confirm
the timeout with your actual tier and upstream first-token behavior.

Disable request/response **body logging** in APIM diagnostics for Application
Insights, Azure Monitor and Event Hubs, including inherited settings. Avoid
response caching, compression/transformation/validation policies that buffer
the stream, and response `set-body`/`Body.As<JObject>()` rewrites. In particular,
the old alias-rewrite example in the historical guide cannot parse SSE as one
JSON document. The BFF supplies the validated alias in its `meta` event; it does
not need a rewritten provider `model` property.

Keep the normal HTTP distinction between recognized gateway token-rate
exhaustion (**429**) and recognized token-quota exhaustion (**403**).
For an upstream 403, the BFF requires JSON `error.code` (or top-level `code`)
matching one of `TokenQuotaExceeded`, `QuotaExceeded`, `quota_exceeded`, or
`token_quota_exceeded` (case-insensitive) before returning application 403
`quota_exceeded`. An arbitrary upstream 403, including a permission failure,
becomes application **502 `upstream_rejected`**, not quota exhaustion.

**Deployment gate:** Microsoft Learn documents quota status 403, but does not
establish this gateway's exact JSON error-code shape. Confirm that shape on the
actual configured APIM policy using an approved isolated test, or have its owner
normalize quota errors to the code contract before release. Do not assume a
status code alone identifies quota exhaustion or deliberately exhaust paid
production quotas to discover the shape.

If normalization requires APIM `on-error` handling, assign an explicit `id` to
the relevant token-limit policy. Scope the handler to that exact
`context.LastError.PolicyId` **and a verified quota-exhaustion reason from that
policy**, distinguishing daily quota from rate limits. Only that proven quota
condition may produce a 403 JSON body such as
`{"error":{"code":"TokenQuotaExceeded"}}`. Preserve unrelated authentication,
authorization, subscription, backend and other policy failures; never use a
generic "any 403" or catch-all error rewrite. Review effective inherited policy
behavior and test both the quota case and an unrelated permission denial.
No quota-normalization policy is deployed by this repository.

Preserve `Retry-After` when present and approximate `x-remaining-quota`
metadata. A stream that has already started cannot switch its HTTP status;
the BFF sends `error` and failed `done` instead.

Before enabling streaming usage per alias, verify its route accepts
`stream_options: {"include_usage": true}` and emits an actual final usage frame.
`STREAM_USAGE_MODELS` defaults empty because provider routes differ. A provider
may report usage without that option; absent final usage remains null with
source `unavailable`, never fabricated zero usage. APIM streaming token-limit
estimates and provider-reported usage are different observations; neither an
estimated quota header nor the UI is an authoritative billing ledger.

## Browser, timeouts and cancellation

The browser uses POST `fetch`, an API bearer access token, and `AbortController`.
Native `EventSource` cannot represent this request; repeated polling changes the
contract and is not the fallback. Consume partial UTF-8/SSE frames safely.
Treat keepalive comments as transport activity, not generated model text.
Missing terminal `done` means interrupted, not completed.

Use exact SWA/custom-domain origins in backend `CORS_ORIGINS`. Allow methods
GET/POST/PATCH/PUT/DELETE/OPTIONS and request headers Authorization/Content-Type;
expose X-Request-ID and Retry-After. The backend is bearer-only, with no
cross-origin session cookies. CORS preflight is public, but all `/api/v1`
application routes require validated authentication. CORS is not an access
control substitute and does not protect non-browser requests.

Defaults are 10 seconds connect, 90 seconds upstream read inactivity, 180
seconds total generation, and 10 seconds between browser keepalives. Browser
heartbeats do not extend upstream read deadlines or every intermediary's
maximum request duration. Test each hop for first-token latency, idle gaps and
absolute time limits. Keep responses bounded rather than promising unlimited
connections; SWA linked `/api` is specifically not in this path.

Stop closes the browser request and upstream stream best-effort. The provider
can still finish or bill work. Final usage may never arrive. Do not automatically
retry generation after an ambiguous disconnect; duplicate durable
`clientMessageId` returns 409 and must not silently invoke a second model call.

## Operations

Use structured metadata such as request ID, model alias, HTTP/generation status,
duration, first-token timing and usage availability. Configure telemetry through
the backend's optional connection string, then inspect exported data before
broad release. Never log prompts, completions, bearer tokens, APIM subscription
keys, raw SDK request/response bodies, or secret-bearing environment dumps.
Do not enable APIM per-user identity dimensions merely because an old example
suggests them; user IDs are sensitive/high-cardinality. Keep quota enforcement
counter keys distinct from telemetry dimensions.

Liveness/readiness are public, cheap initialization/process checks, not paid
provider probes. Unit tests and CI use fakes. Limit any approved end-to-end
model checks to named aliases, short prompts, output caps and an explicit spend
allowance. Budget alerts are notifications, not hard spending stops.

Keep one worker, one minimum/maximum ACA replica, and single-revision traffic.
Process-local concurrency cannot coordinate multiple replicas, and rolling
replacement may overlap processes briefly. Durable history needs initialized
Cosmos resources/data-plane roles, owner-scoped access, and explicit
retention/deletion procedures. Shared concurrency and stronger availability are
future deployment gates, not permission to scale the current app.

`HistoryStore` assumes a **single Cosmos write region** for lease arbitration.
Multi-write-region conflict handling is not implemented; do not enable that
topology for this application. Deduplication is scoped to the validated user
and conversation, and is not an exactly-once inference guarantee. Expired
pending turns recover when messages are read or a new turn starts. A process
crash can lose generated text since the last heartbeat checkpoint.

Browser Stop is explicitly persisted as `cancelled`; plain resource `aclose`
fallback is `interrupted`, and pre-header generation errors are `failed`.
Deletion retains **content-free tombstones and cursor metadata**: removing
conversation content does not physically erase every metadata record. Review
that retention separately rather than assuming automatic TTL cleanup.
Conversation metadata updates remain separate from atomic message-partition
operations, so cross-container finalization/deletion is not one transaction.

## First-party references

- [APIM server-sent events](https://learn.microsoft.com/azure/api-management/how-to-server-sent-events)
- [Forward-request policy](https://learn.microsoft.com/azure/api-management/forward-request-policy)
- [LLM token limits and streaming estimates](https://learn.microsoft.com/azure/api-management/llm-token-limit-policy)
- [Entra claims validation](https://learn.microsoft.com/entra/identity-platform/claims-validation)
