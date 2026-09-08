import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.sse import sse_data


def retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return max(0, min(int(value), 86400))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(0, min(int((date - datetime.now(timezone.utc)).total_seconds()), 86400))
        except (ValueError, TypeError, OverflowError):
            return None


def unavailable_usage() -> dict[str, object]:
    return {"promptTokens": None, "completionTokens": None, "source": "unavailable"}


@dataclass
class GatewayStream:
    response: httpx.Response

    @property
    def remaining_quota(self) -> int | None:
        value = self.response.headers.get("x-remaining-quota")
        try:
            return max(0, int(value)) if value is not None else None
        except ValueError:
            return None

    async def close(self) -> None:
        await self.response.aclose()

    async def events(self) -> AsyncIterator[tuple[str, dict[str, object]]]:
        async for data in sse_data(self.response):
            if data.strip() == "[DONE]":
                yield "complete", {}
                return
            try:
                payload = json.loads(data)
                if not isinstance(payload, dict):
                    raise ValueError("Object expected")
                if payload.get("error"):
                    raise ApiError(502, "upstream_error", "The model interrupted the response.")
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
                    if (
                        isinstance(prompt, int)
                        and not isinstance(prompt, bool)
                        and prompt >= 0
                        and isinstance(completion, int)
                        and not isinstance(completion, bool)
                        and completion >= 0
                    ):
                        yield (
                            "usage",
                            {
                                "promptTokens": prompt,
                                "completionTokens": completion,
                                "source": "provider",
                            },
                        )
                choices = payload.get("choices", [])
                if not isinstance(choices, list):
                    raise ValueError("Invalid choices")
                for choice in choices:
                    if not isinstance(choice, dict):
                        raise ValueError("Invalid choice")
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta", {})
                    if not isinstance(delta, dict):
                        raise ValueError("Invalid delta")
                    content = delta.get("content")
                    if content is not None:
                        if not isinstance(content, str):
                            raise ValueError("Invalid content")
                        if content:
                            yield "delta", {"text": content}
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        if not isinstance(reason, str):
                            raise ValueError("Invalid finish reason")
                        yield "finish", {"finishReason": reason}
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                raise ApiError(
                    502, "invalid_stream", "The gateway returned an invalid event."
                ) from exc
        raise ApiError(502, "stream_interrupted", "The model connection ended before completion.")


class GatewayClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, key: str):
        self.settings = settings
        self.client = client
        self._key = key

    def headers(self, user_id: str, request_id: str) -> dict[str, str]:
        return {
            self.settings.apim_subscription_header: self._key,
            "x-user-id": user_id,
            "x-request-id": request_id,
            "Content-Type": "application/json",
        }

    async def models(self, user_id: str, request_id: str) -> list[dict[str, str]]:
        try:
            response = await self.client.get(
                f"{self.settings.apim_endpoint}/models", headers=self.headers(user_id, request_id)
            )
            if response.status_code != 200:
                raise ApiError(502, "model_discovery_failed", "Model discovery is unavailable.")
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("Invalid model list")
            if any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str)
                for item in payload["data"]
            ):
                raise ValueError("Invalid model entry")
            aliases = {item["id"] for item in payload["data"]}
            return [{"id": alias} for alias in self.settings.allowed_models if alias in aliases]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ApiError(
                502, "model_discovery_failed", "Model discovery is unavailable."
            ) from exc

    async def open_chat(
        self,
        user_id: str,
        request_id: str,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> GatewayStream:
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if model in self.settings.stream_usage_models:
            body["stream_options"] = {"include_usage": True}
        request = self.client.build_request(
            "POST",
            f"{self.settings.apim_endpoint}/chat/completions",
            headers=self.headers(user_id, request_id),
            json=body,
        )
        try:
            response = await self.client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise ApiError(504, "upstream_timeout", "The gateway did not respond in time.") from exc
        except httpx.HTTPError as exc:
            raise ApiError(502, "upstream_unavailable", "The gateway is unavailable.") from exc
        if response.status_code != 200:
            try:
                await self._raise_response_error(response)
            except httpx.HTTPError as exc:
                raise ApiError(
                    502, "upstream_unavailable", "The gateway response was interrupted."
                ) from exc
            finally:
                await response.aclose()
        if "text/event-stream" not in response.headers.get("content-type", "").lower():
            await response.aclose()
            raise ApiError(
                502, "invalid_stream", "The gateway did not return a streaming response."
            )
        return GatewayStream(response)

    async def _raise_response_error(self, response: httpx.Response) -> None:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk[: 8192 - len(body)])
            if len(body) >= 8192:
                break
        code = ""
        try:
            payload = json.loads(body)
            error = payload.get("error", payload)
            code = str(error.get("code", error.get("statusCode", ""))).lower()
        except (ValueError, AttributeError, TypeError):
            pass  # The HTTP failure is still surfaced; never relay untrusted upstream text.
        if response.status_code == 429:
            raise ApiError(
                429,
                "rate_limited",
                "Too many requests. Try again after the retry interval.",
                retry_after(response.headers.get("retry-after")),
            )
        if response.status_code == 403 and code in {
            "tokenquotaexceeded",
            "quotaexceeded",
            "quota_exceeded",
            "token_quota_exceeded",
        }:
            raise ApiError(403, "quota_exceeded", "The model's token allowance has been exhausted.")
        raise ApiError(
            502, "upstream_rejected", "The gateway rejected the request. Contact the administrator."
        )
