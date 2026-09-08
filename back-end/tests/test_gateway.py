import json

import httpx
import pytest

from app.core.errors import ApiError
from app.core.sse import sse_data
from app.services.apim_client import GatewayStream, retry_after


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_utf8_and_frames_can_cross_chunks(newline):
    raw = ('\ufeffdata: {"text":"café"}' + newline * 2 + "data: [DONE]" + newline * 2).encode()
    response = httpx.Response(200, stream=Chunks([raw[i : i + 1] for i in range(len(raw))]))
    assert [item async for item in sse_data(response)] == ['{"text":"café"}', "[DONE]"]


@pytest.mark.parametrize(
    "data",
    [b"data: unfinished", b"data: \xff\n\n", b"x" * 262145],
    ids=["unfinished", "invalid-utf8", "oversized"],
)
async def test_invalid_streams_are_rejected(data):
    with pytest.raises(ApiError):
        async for _ in sse_data(httpx.Response(200, stream=Chunks([data]))):
            pass


async def test_gateway_requires_done_marker():
    payload = {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}
    response = httpx.Response(200, content=f"data: {json.dumps(payload)}\n\n")
    with pytest.raises(ApiError):
        _ = [event async for event in GatewayStream(response).events()]


async def test_usage_zero_is_valid_but_missing_is_not_invented():
    response = httpx.Response(
        200, content='data: {"usage":{"prompt_tokens":0,"completion_tokens":0}}\n\ndata: [DONE]\n\n'
    )
    results = [event async for event in GatewayStream(response).events()]
    assert results[0] == (
        "usage",
        {
            "promptTokens": 0,
            "completionTokens": 0,
            "source": "provider",
        },
    )


@pytest.mark.parametrize("value,expected", [(None, None), ("10", 10), ("-5", 0), ("wrong", None)])
def test_retry_after(value, expected):
    assert retry_after(value) == expected


@pytest.mark.parametrize("models,enabled", [([], False), (["gpt-4o"], True)])
async def test_usage_request_is_only_sent_to_opted_in_models(harness, settings, models, enabled):
    settings.stream_usage_models = models
    stream = await harness.services.gateway.open_chat("user", "request", "gpt-4o", [], 100)
    try:
        assert ("stream_options" in json.loads(harness.requests[-1].content)) is enabled
    finally:
        await stream.close()


@pytest.mark.parametrize("payload", [{"data": {}}, {"data": [None]}, []])
async def test_invalid_model_discovery_is_not_an_empty_success(settings, payload):
    from app.services.apim_client import GatewayClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ApiError) as error:
            await GatewayClient(settings, client, "test-key").models("user", "request")
    assert error.value.code == "model_discovery_failed"
