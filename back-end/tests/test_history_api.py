from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ApiError
from app.main import create_app


class TurnStub:
    message_id = "assistant-test"
    content = ""

    def __init__(self, message):
        self.messages = [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
            {"role": "user", "content": message},
        ]
        self.saved = []
        self.released = False

    def assert_lease(self):
        pass

    async def finish(self, status, content, usage, metrics):
        self.saved.append((status, content, usage, metrics))


class HistoryStub:
    def __init__(self):
        self.calls = []
        self.turn = None

    @asynccontextmanager
    async def open_turn(self, user, conversation, message_id, model, message, prompt):
        self.calls.append(("open_turn", user, conversation, message_id, model, prompt))
        self.turn = TurnStub(message)
        try:
            yield self.turn
        finally:
            self.turn.released = True

    async def create_conversation(self, user, title):
        self.calls.append(("create", user, title))
        return {"id": str(uuid4()), "title": title}

    async def list_conversations(self, user, limit, token):
        self.calls.append(("list", user, limit, token))
        return {"items": [], "continuationToken": None}

    async def rename_conversation(self, user, conversation, title):
        self.calls.append(("rename", user, conversation, title))
        return {"id": conversation, "title": title}

    async def delete_conversation(self, user, conversation):
        self.calls.append(("delete", user, conversation))

    async def list_messages(self, user, conversation, limit, token):
        self.calls.append(("messages", user, conversation, limit, token))
        return {"items": [], "continuationToken": None}

    async def feedback(self, user, conversation, message, rating):
        self.calls.append(("feedback", user, conversation, message, rating))


@pytest.fixture
def history_client(settings, harness):
    history = HistoryStub()
    harness.services.history = history
    with TestClient(create_app(settings, services=harness.services)) as client:
        yield client, history


def test_history_routes_always_supply_authenticated_owner(history_client, token_factory):
    client, history = history_client
    headers = {"Authorization": f"Bearer {token_factory()}", "x-user-id": "forged"}
    conversation = str(uuid4())
    base = f"/api/v1/conversations/{conversation}"
    assert client.get("/api/v1/models", headers=headers).json()["historyEnabled"] is True
    assert (
        client.post("/api/v1/conversations", headers=headers, json={"title": "New"}).status_code
        == 201
    )
    assert (
        client.get(
            "/api/v1/conversations?limit=5&continuationToken=next", headers=headers
        ).status_code
        == 200
    )
    assert client.patch(base, headers=headers, json={"title": "Renamed"}).status_code == 200
    assert client.get(f"{base}/messages?limit=10", headers=headers).status_code == 200
    assert (
        client.put(
            f"{base}/messages/assistant-test/feedback", headers=headers, json={"rating": "up"}
        ).status_code
        == 204
    )
    assert client.delete(base, headers=headers).status_code == 204
    assert all(call[1] == "33333333-3333-4333-8333-333333333333" for call in history.calls)
    assert history.calls[1][2:] == (5, "next")
    assert history.calls[3][3:] == (10, None)


@pytest.mark.parametrize("failure", [False, True])
def test_history_turn_is_finalized_before_terminal_response(
    history_client, harness, token_factory, failure
):
    import json

    client, history = history_client
    if failure:
        harness.state["body"] = 'data: {"error":{"code":"failed"}}\n\n'
    response = client.post(
        f"/api/v1/conversations/{uuid4()}/messages",
        json={"model": "gpt-4o", "message": "Next question", "clientMessageId": str(uuid4())},
        headers={"Authorization": f"Bearer {token_factory()}"},
    )
    assert response.status_code == 200
    assert '"messageId": "assistant-test"' in response.text
    assert history.turn.saved[-1][0] == ("failed" if failure else "completed")
    assert history.turn.saved[-1][3]["requestId"] == response.headers["x-request-id"]
    assert history.turn.released
    upstream = json.loads(harness.requests[-1].content)
    assert upstream["messages"][1:] == history.turn.messages


def test_pre_stream_rejection_persists_failure(history_client, harness, token_factory):
    client, history = history_client
    harness.state.update(status=429, body='{"error":{"code":"RateLimitExceeded"}}')
    response = client.post(
        f"/api/v1/conversations/{uuid4()}/messages",
        json={"model": "luna", "message": "Hi", "clientMessageId": str(uuid4())},
        headers={"Authorization": f"Bearer {token_factory()}"},
    )
    assert response.status_code == 429
    assert history.turn.saved[-1][0] == "failed"
    assert history.turn.released


def test_failed_persistence_never_reports_success(history_client, token_factory, monkeypatch):
    client, history = history_client

    async def cannot_save(self, *args):
        raise ApiError(503, "history_unavailable", "History is unavailable")

    monkeypatch.setattr(TurnStub, "finish", cannot_save)
    response = client.post(
        f"/api/v1/conversations/{uuid4()}/messages",
        json={"model": "luna", "message": "Hi", "clientMessageId": str(uuid4())},
        headers={"Authorization": f"Bearer {token_factory()}"},
    )
    assert '"status": "completed"' not in response.text
    assert '"status": "failed"' in response.text
    assert history.turn.released
