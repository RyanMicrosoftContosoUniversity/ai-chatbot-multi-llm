import json
import time
from dataclasses import dataclass

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.api.deps import Services
from app.core.auth import TokenValidator
from app.core.config import Settings
from app.services.apim_client import GatewayClient

TENANT = "11111111-1111-4111-8111-111111111111"
API = "22222222-2222-4222-8222-222222222222"
USER = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        entra_tenant_id=TENANT,
        entra_api_client_id=API,
        apim_endpoint="https://gateway.example/llm/v1",
        apim_subscription_key="test-only-not-a-real-credential",
    )


@pytest.fixture(scope="session")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def token_factory(signing_key):
    def create(**overrides):
        now = int(time.time())
        claims = {
            "aud": API,
            "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
            "tid": TENANT,
            "oid": USER,
            "ver": "2.0",
            "iat": now,
            "nbf": now - 5,
            "exp": now + 600,
            "scp": "chat.access",
        }
        claims.update(overrides)
        return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test-key"})

    return create


def completion(text="Hello", usage=True):
    values = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    if usage:
        values.append({"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2}})
    return "".join(f"data: {json.dumps(value)}\n\n" for value in values) + "data: [DONE]\n\n"


@dataclass
class BackendHarness:
    services: Services
    client: httpx.AsyncClient
    requests: list
    state: dict


@pytest.fixture
def harness(settings, signing_key):
    requests = []
    state = {"body": completion(), "status": 200, "content_type": "text/event-stream"}
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    public_jwk["kid"] = "test-key"

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": settings.issuer,
                    "jwks_uri": f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys",
                },
            )
        if request.url.path.endswith("/keys"):
            return httpx.Response(200, json={"keys": [public_jwk]})
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "gpt-4o"},
                        {"id": "luna"},
                        {"id": "deepseek"},
                        {"id": "not-allowed"},
                    ]
                },
            )
        return httpx.Response(
            state["status"],
            content=state["body"],
            headers={
                "content-type": state["content_type"],
                "x-remaining-quota": "1200",
                "retry-after": "7",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    services = Services(
        TokenValidator(settings, client),
        GatewayClient(settings, client, settings.apim_subscription_key.get_secret_value()),
    )
    return BackendHarness(services, client, requests, state)
