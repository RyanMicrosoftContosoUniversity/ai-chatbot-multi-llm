import json
import logging

import pytest
from conftest import completion
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(settings, harness):
    with TestClient(create_app(settings, services=harness.services)) as client:
        yield client


@pytest.fixture
def auth(token_factory):
    return {"Authorization": f"Bearer {token_factory()}", "Origin": "http://localhost:5173"}


def test_health_and_auth_boundary(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert client.get("/api/v1/models").status_code == 401
    assert client.get("/api/v1/conversations").status_code == 401


def test_allowed_models(client, auth):
    response = client.get("/api/v1/models", headers=auth)
    assert response.status_code == 200
    assert response.json() == {
        "models": [{"id": "gpt-4o"}, {"id": "luna"}, {"id": "deepseek"}],
        "historyEnabled": False,
    }


def test_chat_contract_and_identity(client, harness, auth):
    response = client.post(
        "/api/v1/chat",
        json={"model": "gpt-4o", "message": "Hi"},
        headers={
            **auth,
            "x-user-id": "forged",
            "x-request-id": "untrusted",
        },
    )
    assert response.status_code == 200
    assert "event: meta" in response.text
    assert '"text": "Hello"' in response.text
    assert '"source": "provider"' in response.text
    assert response.text.count("event: done") == 1
    upstream = harness.requests[-1]
    assert upstream.headers["x-user-id"] == "33333333-3333-4333-8333-333333333333"
    assert upstream.headers["x-request-id"] == response.headers["x-request-id"]
    body = json.loads(upstream.content)
    assert body["messages"][0]["role"] == "system"
    assert body["max_tokens"] == 2000
    assert "authorization" not in upstream.headers
    assert "test-only-not-a-real-credential" not in response.text
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.parametrize(
    "body,status,code",
    [
        ({"model": "evil", "message": "Hi"}, 400, "unknown_model"),
        ({"model": "gpt-4o", "message": "Hi", "maxTokens": 2001}, 422, "token_limit"),
        ({"model": "gpt-4o", "message": " "}, 422, "invalid_request"),
        ({"model": "gpt-4o", "message": "Hi", "messages": []}, 422, "invalid_request"),
        ({"model": "gpt-4o", "message": "x" * 12001}, 413, "message_too_large"),
    ],
)
def test_input_limits(client, auth, body, status, code):
    response = client.post("/api/v1/chat", json=body, headers=auth)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_body_limit(client, auth):
    response = client.post("/api/v1/chat", content=b"x" * 70000, headers=auth)
    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_preflight(client):
    response = client.options(
        "/api/v1/chat",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert (
        client.options(
            "/api/v1/chat",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
            },
        ).status_code
        == 400
    )


def test_auth_errors_include_cors(client):
    response = client.get("/api/v1/models", headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["www-authenticate"] == "Bearer"


def test_early_rate_limit_is_http_error(client, harness, auth):
    harness.state.update(status=429, body='{"error":{"code":"RateLimitExceeded"}}')
    response = client.post("/api/v1/chat", json={"model": "luna", "message": "Hi"}, headers=auth)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert response.headers["retry-after"] == "7"
    harness.state.update(status=200, body=completion())
    assert (
        client.post(
            "/api/v1/chat",
            json={
                "model": "luna",
                "message": "Retry manually",
            },
            headers=auth,
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "upstream_code,expected_status,expected_code",
    [
        ("TokenQuotaExceeded", 403, "quota_exceeded"),
        ("PermissionDenied", 502, "upstream_rejected"),
    ],
)
def test_quota_is_not_every_403(
    client, harness, auth, upstream_code, expected_status, expected_code
):
    harness.state.update(status=403, body=json.dumps({"error": {"code": upstream_code}}))
    response = client.post("/api/v1/chat", json={"model": "gpt-4o", "message": "Hi"}, headers=auth)
    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code


def test_mid_stream_failure_is_not_success(client, harness, auth):
    harness.state["body"] = 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
    response = client.post("/api/v1/chat", json={"model": "gpt-4o", "message": "Hi"}, headers=auth)
    assert response.status_code == 200
    assert "event: error" in response.text
    assert '"status": "failed"' in response.text
    assert '"status": "completed"' not in response.text


def test_missing_usage_remains_unknown(client, harness, auth):
    harness.state["body"] = completion(usage=False)
    response = client.post(
        "/api/v1/chat", json={"model": "deepseek", "message": "Hi"}, headers=auth
    )
    assert '"source": "unavailable"' in response.text
    assert '"promptTokens": null' in response.text


def test_history_disabled_is_explicit(client, auth):
    response = client.get("/api/v1/conversations", headers=auth)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "history_disabled"


def test_readiness_lifecycle(settings, harness):
    app = create_app(settings, services=harness.services)
    assert app.state.ready is False
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
    assert app.state.ready is False


def test_chat_logs_metadata_without_prompts_credentials_or_output(client, harness, auth, caplog):
    caplog.set_level(logging.INFO, logger="multillm_bff")
    harness.state["body"] = completion("sensitive-output-test")
    response = client.post(
        "/api/v1/chat",
        json={"model": "gpt-4o", "message": "sensitive-input-test"},
        headers=auth,
    )
    assert response.status_code == 200
    assert "chat_end" in caplog.text
    assert "ttft_ms=" in caplog.text
    assert "sensitive-input-test" not in caplog.text
    assert "sensitive-output-test" not in caplog.text
    assert "test-only-not-a-real-credential" not in caplog.text
    assert auth["Authorization"] not in caplog.text
