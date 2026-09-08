import asyncio
import socket
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
import uvicorn
from starlette.requests import ClientDisconnect

from app.core.sse import OwnedStreamingResponse
from app.main import create_app


class SlowModel(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = asyncio.Event()

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"First chunk"}}]}\n\n'
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed.set()


@asynccontextmanager
async def server_for(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Test server did not start")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


def slow_transport(harness, stream):
    original = harness.client._transport.handler

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})
        return original(request)

    harness.client._transport.handler = handler


async def test_browser_disconnect_closes_upstream_and_releases_capacity(
    settings, harness, token_factory
):
    settings.heartbeat_seconds = 0.02
    stream = SlowModel()
    slow_transport(harness, stream)
    app = create_app(settings, services=harness.services)
    headers = {"Authorization": f"Bearer {token_factory()}"}
    async with server_for(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as browser:
            async with browser.stream(
                "POST",
                "/api/v1/chat",
                json={"model": "gpt-4o", "message": "Hi"},
                headers=headers,
            ) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if "First chunk" in line:
                        break
            await asyncio.wait_for(stream.closed.wait(), 2)
            replacement = SlowModel()
            slow_transport(harness, replacement)
            async with browser.stream(
                "POST",
                "/api/v1/chat",
                json={"model": "gpt-4o", "message": "Again"},
                headers=headers,
            ) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if "First chunk" in line:
                        break
            await asyncio.wait_for(replacement.closed.wait(), 2)


async def test_overall_timeout_is_terminal_and_closes_upstream(settings, harness, token_factory):
    settings.generation_timeout_seconds = 0.06
    settings.heartbeat_seconds = 0.01
    stream = SlowModel()
    slow_transport(harness, stream)
    app = create_app(settings, services=harness.services)
    async with server_for(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as browser:
            response = await browser.post(
                "/api/v1/chat",
                json={"model": "luna", "message": "Hi"},
                headers={"Authorization": f"Bearer {token_factory()}"},
            )
    assert response.status_code == 200
    assert "event: delta" in response.text
    assert "generation_timeout" in response.text
    assert '"status": "failed"' in response.text
    assert response.text.count("event: done") == 1
    assert stream.closed.is_set()


async def test_upstream_header_wait_obeys_overall_deadline(settings, harness, token_factory):
    settings.generation_timeout_seconds = 0.04
    original = harness.client._transport.handler

    async def handler(request):
        if request.method == "POST":
            await asyncio.Event().wait()
        return original(request)

    harness.client._transport.handler = handler
    async with server_for(create_app(settings, services=harness.services)) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as browser:
            response = await browser.post(
                "/api/v1/chat",
                json={"model": "luna", "message": "Hi"},
                headers={"Authorization": f"Bearer {token_factory()}"},
            )
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "generation_timeout"


async def test_response_owns_cleanup_before_generator_entry():
    resources = AsyncExitStack()
    closed = []
    entered = []
    resources.callback(closed.append, True)

    async def content():
        entered.append(True)
        yield b"data: test\n\n"

    async def send(message):
        raise OSError("Disconnected before response headers")

    async def receive():
        return {"type": "http.disconnect"}

    response = OwnedStreamingResponse(content(), resources, "request-id")
    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert closed == [True]
    assert entered == []
