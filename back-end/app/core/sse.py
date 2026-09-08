import asyncio
import codecs
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack

import anyio
import httpx
from starlette.responses import StreamingResponse

from .errors import ApiError

MAX_FRAME_BYTES = 262144


def event(name: str, data: dict[str, object]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


class OwnedStreamingResponse(StreamingResponse):
    def __init__(self, content, resources: AsyncExitStack, request_id: str):
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Request-ID": request_id},
        )
        self.resources = resources

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A disconnect can precede generator entry; response owns all acquired resources.
            with anyio.CancelScope(shield=True):
                await asyncio.wait_for(self.resources.aclose(), timeout=15)


async def sse_data(response: httpx.Response) -> AsyncIterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    pending = ""
    after_cr = False
    data: list[str] = []
    frame_size = 0
    async for chunk in response.aiter_bytes():
        try:
            decoded = decoder.decode(chunk)
            if decoded:
                if after_cr and decoded.startswith("\n"):
                    decoded = decoded[1:]
                after_cr = decoded.endswith("\r")
                pending += decoded.replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as exc:
            raise ApiError(
                502, "invalid_stream", "The gateway returned an invalid stream."
            ) from exc
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            frame_size += len(line.encode("utf-8"))
            if frame_size > MAX_FRAME_BYTES:
                raise ApiError(502, "invalid_stream", "The gateway returned an oversized event.")
            if line == "":
                if data:
                    yield "\n".join(data)
                data = []
                frame_size = 0
            elif line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
        if len(pending.encode("utf-8")) + frame_size > MAX_FRAME_BYTES:
            raise ApiError(502, "invalid_stream", "The gateway returned an oversized event.")
    try:
        pending += decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise ApiError(502, "invalid_stream", "The gateway returned an incomplete stream.") from exc
    if pending.strip() or data:
        raise ApiError(502, "invalid_stream", "The gateway stream ended inside an event.")
