import json
from importlib.resources import files
from unittest.mock import AsyncMock

from fastapi import Response
from fastapi.testclient import TestClient

from app.api.deps import chat_service
from app.main import create_app
from app.services.chat_service import ChatService

CONVERSATION = "44444444-4444-4444-8444-444444444444"
MESSAGE = "55555555-5555-4555-8555-555555555555"


def test_registered_api_surface_is_unchanged(settings, harness):
    app = create_app(settings, services=harness.services)
    paths = app.openapi()["paths"]
    assert {path: set(methods) for path, methods in paths.items()} == {
        "/health/live": {"get"},
        "/health/ready": {"get"},
        "/api/v1/models": {"get"},
        "/api/v1/chat": {"post"},
        "/api/v1/conversations": {"get", "post"},
        "/api/v1/conversations/{conversation_id}": {"patch", "delete"},
        "/api/v1/conversations/{conversation_id}/messages": {"get", "post"},
        "/api/v1/conversations/{conversation_id}/messages/{message_id}/feedback": {"put"},
    }


def test_both_chat_routes_use_service_dependency(settings, harness, token_factory):
    service = AsyncMock(spec=ChatService)
    service.start_chat.return_value = Response(status_code=202)
    app = create_app(settings, services=harness.services)
    app.dependency_overrides[chat_service] = lambda: service
    headers = {"Authorization": f"Bearer {token_factory()}"}
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/v1/chat", json={"model": "luna", "message": "Hi"}, headers=headers
            ).status_code
            == 202
        )
        assert (
            client.post(
                f"/api/v1/conversations/{CONVERSATION}/messages",
                json={"model": "luna", "message": "Again", "clientMessageId": MESSAGE},
                headers=headers,
            ).status_code
            == 202
        )
    single_turn, conversation_turn = service.start_chat.await_args_list
    assert single_turn.args[0].message == "Hi"
    assert len(single_turn.args) == 3
    assert str(conversation_turn.args[0].clientMessageId) == MESSAGE
    assert conversation_turn.args[3] == CONVERSATION


def test_app_instances_keep_configuration_and_capacity_separate(settings, harness, token_factory):
    first = create_app(settings, services=harness.services)
    second_settings = settings.model_copy(update={"max_tokens_cap": 123})
    second = create_app(second_settings, services=harness.services)
    with TestClient(first) as first_client, TestClient(second) as second_client:
        assert first.state.chat_service is not second.state.chat_service
        assert first.state.chat_service.capacity is not second.state.chat_service.capacity
        for client, expected_cap in [(first_client, settings.max_tokens_cap), (second_client, 123)]:
            response = client.post(
                "/api/v1/chat",
                json={"model": "luna", "message": "Hi"},
                headers={"Authorization": f"Bearer {token_factory()}"},
            )
            assert response.status_code == 200
            payload = json.loads(harness.requests[-1].content)
            assert payload["max_tokens"] == expected_cap
            assert payload["messages"][0] == {
                "role": "system",
                "content": files("app")
                .joinpath("prompts", "system_v1.txt")
                .read_text(encoding="utf-8")
                .strip(),
            }
