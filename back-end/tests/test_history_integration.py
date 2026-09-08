import asyncio
import json
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from test_history import FakeDatabase
from test_streaming_live import SlowModel, server_for, slow_transport

from app.main import create_app
from app.repositories.chat_repository import HistoryStore


def test_durable_conversation_api_round_trip(settings, harness, token_factory):
    harness.services.history = HistoryStore(FakeDatabase(settings), settings)
    headers = {"Authorization": f"Bearer {token_factory()}"}
    with TestClient(create_app(settings, services=harness.services)) as client:
        created = client.post("/api/v1/conversations", json={"title": "First"}, headers=headers)
        assert created.status_code == 201
        base = f"/api/v1/conversations/{created.json()['id']}"
        body = {"model": "gpt-4o", "message": "Hi", "clientMessageId": str(uuid4())}
        response = client.post(f"{base}/messages", json=body, headers=headers)
        assert response.status_code == 200
        assert '"status": "completed"' in response.text
        events = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        message_id = events[0]["messageId"]
        messages = client.get(f"{base}/messages", headers=headers).json()["items"]
        assert len(messages) == 2
        assert all(message["status"] == "completed" for message in messages)
        assert (
            client.put(
                f"{base}/messages/{message_id}/feedback",
                json={"rating": "up"},
                headers=headers,
            ).status_code
            == 204
        )
        messages = client.get(f"{base}/messages", headers=headers).json()["items"]
        assert next(item for item in messages if item["id"] == message_id)["feedback"] == "up"
        assert client.post(f"{base}/messages", json=body, headers=headers).status_code == 409
        assert len([request for request in harness.requests if request.method == "POST"]) == 1
        next_body = {**body, "clientMessageId": str(uuid4()), "message": "Continue"}
        assert client.post(f"{base}/messages", json=next_body, headers=headers).status_code == 200
        sent = json.loads(harness.requests[-1].content)["messages"]
        assert [item["role"] for item in sent] == ["system", "user", "assistant", "user"]
        other = {"Authorization": f"Bearer {token_factory(oid=str(uuid4()))}"}
        assert client.get(f"{base}/messages", headers=other).status_code == 404
        assert client.get("/api/v1/conversations", headers=other).json()["items"] == []
        assert client.patch(base, json={"title": "Renamed"}, headers=headers).status_code == 200
        assert (
            client.get("/api/v1/conversations", headers=headers).json()["items"][0]["title"]
            == "Renamed"
        )
        assert client.delete(base, headers=headers).status_code == 204
        assert client.get("/api/v1/conversations", headers=headers).json()["items"] == []
        assert client.get(f"{base}/messages", headers=headers).status_code == 404


async def test_browser_stop_persists_cancelled_partial_response(settings, harness, token_factory):
    settings.heartbeat_seconds = 0.02
    harness.services.history = HistoryStore(FakeDatabase(settings), settings)
    stream = SlowModel()
    slow_transport(harness, stream)
    headers = {"Authorization": f"Bearer {token_factory()}"}
    async with server_for(create_app(settings, services=harness.services)) as base_url:
        async with httpx.AsyncClient(base_url=base_url, headers=headers) as client:
            created = await client.post("/api/v1/conversations", json={"title": "Stop test"})
            base = f"/api/v1/conversations/{created.json()['id']}/messages"
            async with client.stream(
                "POST",
                base,
                json={"model": "luna", "message": "Hi", "clientMessageId": str(uuid4())},
            ) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if "First chunk" in line:
                        break
            await asyncio.wait_for(stream.closed.wait(), 2)
            async with asyncio.timeout(3):
                while True:
                    page = await client.get(base)
                    assert page.status_code == 200
                    assistant = next(
                        item for item in page.json()["items"] if item["role"] == "assistant"
                    )
                    if assistant["status"] != "pending":
                        break
                    await asyncio.sleep(0.02)
    assert assistant["status"] == "cancelled"
    assert assistant["content"] == "First chunk"
    assert assistant["usage"]["source"] == "unavailable"
