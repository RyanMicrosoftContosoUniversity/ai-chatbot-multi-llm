import asyncio
import logging
import time
from contextlib import AsyncExitStack, suppress
from importlib.resources import files
from typing import TYPE_CHECKING

import anyio
import httpx
from fastapi import Request

from app.core.auth import User
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.sse import OwnedStreamingResponse, event
from app.domain.schemas import ChatRequest, ConversationChatRequest

from .apim_client import GatewayClient, GatewayStream, unavailable_usage
from .concurrency import Capacity

if TYPE_CHECKING:
    from app.repositories.chat_repository import HistoryStore, Turn

logger = logging.getLogger("multillm_bff")


class ChatService:
    def __init__(
        self,
        settings: Settings,
        gateway: GatewayClient,
        capacity: Capacity,
        history: "HistoryStore | None" = None,
    ):
        self.settings = settings
        self.gateway = gateway
        self.capacity = capacity
        self.history = history
        self.system_prompt = (
            files("app").joinpath("prompts", "system_v1.txt").read_text(encoding="utf-8").strip()
        )

    async def start_chat(
        self,
        body: ChatRequest,
        request: Request,
        user: User,
        conversation_id: str | None = None,
    ) -> OwnedStreamingResponse:
        configuration = self.settings
        if body.model not in configuration.allowed_models:
            raise ApiError(400, "unknown_model", "Select a supported model alias.")
        if len(body.message) > configuration.max_message_chars:
            raise ApiError(413, "message_too_large", "The message exceeds the input size limit.")
        max_tokens = body.maxTokens if body.maxTokens is not None else configuration.max_tokens_cap
        if max_tokens > configuration.max_tokens_cap:
            raise ApiError(422, "token_limit", "Requested output exceeds the server token cap.")
        resources = AsyncExitStack()
        started = time.monotonic()
        turn = None
        try:
            await resources.enter_async_context(self.capacity.acquire(user.oid))
            if conversation_id is not None:
                if not isinstance(body, ConversationChatRequest):
                    raise RuntimeError("Conversation request type required")
                if self.history is None:
                    raise ApiError(
                        503, "history_disabled", "Conversation history is not configured."
                    )
                turn = await resources.enter_async_context(
                    self.history.open_turn(
                        user.oid,
                        conversation_id,
                        str(body.clientMessageId),
                        body.model,
                        body.message,
                        "system-v1",
                    )
                )
                turn.assert_lease()
            messages = [{"role": "system", "content": self.system_prompt}]
            messages.extend(turn.messages if turn else [{"role": "user", "content": body.message}])
            if sum(len(item["content"]) for item in messages) > configuration.max_context_chars:
                raise ApiError(413, "context_too_large", "Conversation exceeds the context limit.")
            remaining = configuration.generation_timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise ApiError(504, "generation_timeout", "The generation exceeded its time limit.")
            try:
                async with asyncio.timeout(remaining):
                    upstream = await self.gateway.open_chat(
                        user.oid, request.state.request_id, body.model, messages, max_tokens
                    )
            except TimeoutError as exc:
                raise ApiError(
                    504, "generation_timeout", "The generation exceeded its time limit."
                ) from exc
            resources.push_async_callback(upstream.close)
            return OwnedStreamingResponse(
                chat_events(
                    request, upstream, configuration, body.model, started, turn, conversation_id
                ),
                resources,
                request.state.request_id,
            )
        except BaseException as exc:
            # Release acquired resources even if cancellation happens before response creation.
            with anyio.CancelScope(shield=True):
                try:
                    if turn is not None and not isinstance(exc, asyncio.CancelledError):
                        await asyncio.wait_for(
                            turn.finish(
                                "failed",
                                turn.content,
                                unavailable_usage(),
                                {
                                    "requestId": request.state.request_id,
                                    "latencyMs": round((time.monotonic() - started) * 1000),
                                },
                            ),
                            timeout=10,
                        )
                finally:
                    await asyncio.wait_for(resources.aclose(), timeout=15)
            raise


async def chat_events(
    request: Request,
    upstream: GatewayStream,
    settings: "Settings",
    model: str,
    started: float,
    turn: "Turn | None" = None,
    conversation_id: str | None = None,
):
    request_id = request.state.request_id
    meta: dict[str, object] = {
        "version": "1",
        "requestId": request_id,
        "model": model,
        "remainingQuota": upstream.remaining_quota,
    }
    if turn is not None:
        meta.update(conversationId=conversation_id, messageId=turn.message_id)
    yield event("meta", meta)
    usage = unavailable_usage()
    content = ""
    status = "cancelled"
    metrics: dict[str, object] = {
        "requestId": request_id,
        "ttftMs": None,
        "finishReason": None,
        "promptVersion": "system-v1",
    }
    pending: asyncio.Task | None = None
    iterator = upstream.events().__aiter__()
    try:
        while True:
            if await request.is_disconnected():
                return
            if turn is not None:
                turn.assert_lease()
            remaining = settings.generation_timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise ApiError(504, "generation_timeout", "The generation exceeded its time limit.")
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            ready, _ = await asyncio.wait(
                {pending}, timeout=min(settings.heartbeat_seconds, remaining)
            )
            if not ready:
                yield b": keepalive\n\n"
                continue
            try:
                name, payload = pending.result()
            except StopAsyncIteration as exc:
                raise ApiError(
                    502, "stream_interrupted", "The model stream ended unexpectedly."
                ) from exc
            finally:
                pending = None
            if name == "delta":
                text = payload["text"]
                if not isinstance(text, str):
                    raise ApiError(502, "invalid_stream", "The model returned invalid text.")
                content += text
                if len(content) > settings.max_tokens_cap * 32:
                    raise ApiError(
                        502, "output_limit", "The model response exceeded the size limit."
                    )
                if turn is not None:
                    turn.content = content
                if metrics["ttftMs"] is None:
                    metrics["ttftMs"] = round((time.monotonic() - started) * 1000)
                yield event("delta", payload)
            elif name == "usage":
                usage = payload
            elif name == "finish":
                metrics.update(payload)
            elif name == "complete":
                metrics["latencyMs"] = round((time.monotonic() - started) * 1000)
                if turn is not None:
                    await turn.finish("completed", content, usage, metrics)
                status = "completed"
                yield event("usage", usage)
                yield event(
                    "done",
                    {"status": "completed", "messageId": turn.message_id if turn else None},
                )
                return
    except (ApiError, httpx.HTTPError, TimeoutError) as exc:
        status = "failed"
        if isinstance(exc, ApiError):
            error = exc
        elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            error = ApiError(504, "upstream_timeout", "The model response timed out.")
        else:
            error = ApiError(502, "stream_interrupted", "The gateway connection was interrupted.")
        metrics["latencyMs"] = round((time.monotonic() - started) * 1000)
        if turn is not None:
            try:
                await turn.finish("failed", content, usage, metrics)
            except ApiError as save_error:
                error = save_error
        yield event("error", error.payload(request_id))
        yield event("done", {"status": "failed", "messageId": turn.message_id if turn else None})
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        await iterator.aclose()
        if turn is not None and status == "cancelled":
            metrics["latencyMs"] = round((time.monotonic() - started) * 1000)
            try:
                with anyio.CancelScope(shield=True):
                    await asyncio.wait_for(turn.finish("cancelled", content, usage, metrics), 10)
            except (ApiError, TimeoutError) as exc:
                logger.error(
                    "cancellation_save_failed request_id=%s error_type=%s",
                    request_id,
                    type(exc).__name__,
                )
        logger.info(
            "chat_end request_id=%s model=%s status=%s duration_ms=%d ttft_ms=%s "
            "usage_source=%s prompt_tokens=%s completion_tokens=%s",
            request_id,
            model,
            status,
            (time.monotonic() - started) * 1000,
            metrics["ttftMs"],
            usage["source"],
            usage["promptTokens"],
            usage["completionTokens"],
        )
