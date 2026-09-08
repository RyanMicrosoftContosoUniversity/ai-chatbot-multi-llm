# Application API v1

The React browser client calls the Container Apps URL directly. Static Web Apps
serves assets only. All `/api/v1` requests require a delegated Entra API access
token with `chat.access` in the configured tenant. Public probes are
`GET /health/live` and `GET /health/ready`.

JSON uses camelCase. Unknown request fields are rejected. The server supplies
the system prompt and user identity. It never accepts a browser transcript,
system message, Azure credential, or provider endpoint.

## Single-turn chat

- `GET /api/v1/models` returns
  `{"models":[{"id":"gpt-4o"},{"id":"luna"},{"id":"deepseek"}],"historyEnabled":false}`.
  The aliases are the intersection of gateway discovery and the backend allowlist.
- `POST /api/v1/chat`: `{"model":"gpt-4o","message":"Hello","maxTokens":500}`.
  `maxTokens` is optional and cannot exceed the backend cap.
- Requests have a server-generated `X-Request-ID`, also returned in errors.
- Before streaming, errors are HTTP responses:
  `{"error":{"code":"rate_limited","message":"...","requestId":"...","retryAfter":10}}`.
  `retryAfter` is optional. Recognized token-rate exhaustion is 429; recognized
  daily token-quota exhaustion is 403. Other upstream 403 responses are not
  reported as quota exhaustion.

## Streaming

Use POST `fetch`, `Authorization: Bearer ...`, JSON content type, and an
`AbortController`. Do not use native `EventSource`. SSE is UTF-8; network chunks
can split characters or events. Each event has `event: NAME` and JSON `data`.

| Event | Data |
| --- | --- |
| `meta` | `{version:"1",requestId,model,remainingQuota:null|number,conversationId?:string,messageId?:string}` |
| `delta` | `{text:string}` |
| `usage` | `{promptTokens:number|null,completionTokens:number|null,source:"provider"|"unavailable"}` |
| `error` | `{code,message,requestId,retryAfter?:number}` |
| `done` | `{status:"completed"|"failed",messageId:string|null}` |

SSE comments are keepalives, not messages. A connected stream terminates once;
an error is followed by failed `done`, never successful `done`. An aborted
connection cannot receive terminal events. Missing `done` means interruption,
not success. Usage may be unavailable after cancellation or provider failure.
Quota metadata is an approximate gateway snapshot, never an exact balance.
Closing the upstream stream is best-effort cancellation, not a billing guarantee.

## Optional durable history

`historyEnabled` is true only when Cosmos is configured. Otherwise conversation
endpoints return a clear `history_disabled` error. Single-turn chat stays usable.

| Method/path under `/api/v1` | Input / output |
| --- | --- |
| `POST /conversations` | `{title?:string}` -> conversation (201) |
| `GET /conversations?limit=20&continuationToken=...` | `{items:Conversation[],continuationToken:string|null}` |
| `PATCH /conversations/{id}` | `{title:string}` -> conversation |
| `DELETE /conversations/{id}` | 204 |
| `GET /conversations/{id}/messages?limit=50&continuationToken=...` | `{items:Message[],continuationToken:string|null}` |
| `POST /conversations/{id}/messages` | `{clientMessageId:UUID,model,message,maxTokens?:number}` -> same SSE contract |
| `PUT /conversations/{id}/messages/{messageId}/feedback` | `{rating:"up"|"down"|null}` -> 204 |

Conversation: `{id,title,createdAt,updatedAt}`.
Message: `{id,conversationId,role:"user"|"assistant",content,status,createdAt,
model?:string,usage?:object,feedback?:"up"|"down"|null}`.
Status is `pending`, `completed`, `failed`, `cancelled`, or `interrupted`.
Clients must tolerate additional response metadata.

All data is owner-scoped using the validated user identity. Duplicate
`clientMessageId` returns 409; it must not silently invoke the model again.
History is loaded by the server. No TTL is enabled automatically; users delete
conversations explicitly. Deletion and interrupted-generation recovery are
idempotent because cross-container Cosmos operations are not atomic.

## Compatibility and deployment

Additive response fields are allowed within v1. New required fields, changed
event semantics, or removed endpoints require a new API version. Frontend and
backend deploy separately. CORS must expose `X-Request-ID` and `Retry-After`;
only exact configured frontend origins are allowed. Browser-visible settings
are public; gateway keys and data-service credentials never appear in them.
