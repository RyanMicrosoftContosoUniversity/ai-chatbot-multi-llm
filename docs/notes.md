> **Historical discussion summary.** The single-container and build-order
> recommendations below are superseded by [the current README](../README.md),
> [split deployment](split-deployment.md) and [API contract](api-contract.md).
> Do not copy old plans to the root or infer current resource state from this note.

Design notes written (not code — you're implementing this yourself). Plan mode blocked writing to the repo root, so it's staged in the session folder; approving this copies it to  .\plan.md .
The finding that changes your design: APIM has a unified model API (preview) — one OpenAI-compatible endpoint that auto-translates to Anthropic Messages format. Your three models don't share a wire protocol, so this replaces an adapter layer you'd otherwise hand-
write.
That forces a SKU decision: AI gateway LLM policies support the Anthropic Messages API only in APIM v2 tiers. The reflexive "it's just a POC, use Developer tier" choice would cost you Claude support in token limiting and semantic caching. → Basic v2 or Standard v2.
Your three questions, answered in depth in the doc:
 - BFF (§4) — what it is, and why an APIM subscription key in a browser makes your per-user limit unenforceable. Includes the tension in your R5: per-user keys and browser safety only coexist via a BFF, and a  counter-key  on a validated claim gets you per-user 
limits with zero provisioning.
 - Streaming (§5) — SSE mechanics, TTFT, and six architectural costs. Notably: under  stream: true , APIM always estimates tokens, so metering degrades. Recommendation: build non-streaming first, add streaming as Phase 7.
 - ACA vs SWA+ACA (§8) — recommending ACA only: same-origin (no CORS), one deploy unit, fewer proxies to break streaming.
Also covered: semantic caching vs prompt caching are unrelated (§6) —  vary-by  is a security control, and  score-threshold  is inverted (lower = stricter, >0.2 risks wrong answers); Cosmos message-per-document (§7, the 2 MB doc limit kills the array approach).
Biggest gap I found (§9.1): a token is not a unit of cost. One flat quota treats an Opus token and a GPT-4o token identically — a user can drain your budget while looking well within quota.
14 todos tracked in SQL, with dependencies. 8 open questions in §11 — the per-user-subscription one is the biggest.