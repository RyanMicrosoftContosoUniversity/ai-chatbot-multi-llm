from uuid import uuid4

from starlette.responses import JSONResponse


class RequestBoundary:
    def __init__(self, app, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ]
                message["headers"] = headers + [(b"x-request-id", request_id.encode())]
            await send(message)

        if scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send_with_id)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_body_bytes:
                response = JSONResponse(
                    {
                        "error": {
                            "code": "request_too_large",
                            "message": "Request body exceeds the size limit.",
                            "requestId": request_id,
                        }
                    },
                    status_code=413,
                )
                await response(scope, receive, send_with_id)
                return
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send_with_id)
