import json

import httpx
import jwt
import pytest

from app.core.auth import TokenValidator
from app.core.errors import ApiError


async def test_valid_token_and_cached_keys(harness, token_factory):
    validator = harness.services.validator
    token = token_factory()
    user = await validator.validate(f"Bearer {token}")
    assert user.oid == "33333333-3333-4333-8333-333333333333"
    await validator.validate(f"Bearer {token}")
    assert len(harness.requests) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "wrong-api"},
        {"iss": "https://attacker.example"},
        {"tid": "44444444-4444-4444-8444-444444444444"},
        {"exp": 1},
        {"nbf": 9999999999},
        {"oid": "not-a-guid"},
        {"ver": "1.0"},
        {"aud": ["22222222-2222-4222-8222-222222222222", "other"]},
    ],
)
async def test_invalid_claims_rejected(harness, token_factory, overrides):
    with pytest.raises(ApiError) as failure:
        await harness.services.validator.validate(f"Bearer {token_factory(**overrides)}")
    assert failure.value.status == 401


@pytest.mark.parametrize("scope", [None, "", "other.access", "chat.access.fake", ["chat.access"]])
async def test_delegated_scope_required(harness, token_factory, scope):
    with pytest.raises(ApiError) as failure:
        await harness.services.validator.validate(f"Bearer {token_factory(scp=scope)}")
    assert failure.value.status == 403


@pytest.mark.parametrize("value", [None, "", "Basic abc", "Bearer malformed", "Bearer "])
async def test_bad_authorization(harness, value):
    with pytest.raises(ApiError) as failure:
        await harness.services.validator.validate(value)
    assert failure.value.status == 401


async def test_symmetric_token_not_accepted(harness):
    token = jwt.encode(
        {"scp": "chat.access"}, "test-only-not-a-real-signing-key-123456", algorithm="HS256"
    )
    with pytest.raises(ApiError):
        await harness.services.validator.validate(f"Bearer {token}")
    assert not harness.requests


async def test_identity_outage_fails_closed(settings, token_factory):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        with pytest.raises(ApiError) as failure:
            await TokenValidator(settings, client).validate(f"Bearer {token_factory()}")
        assert failure.value.status == 503


async def test_untrusted_jwks_url_rejected(settings, token_factory):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "issuer": settings.issuer,
                "jwks_uri": "https://attacker.example/keys",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ApiError):
            await TokenValidator(settings, client).validate(f"Bearer {token_factory()}")
    assert len(requests) == 1


async def test_unknown_kid_does_not_trigger_request_storm(harness, token_factory, signing_key):
    validator = harness.services.validator
    valid = token_factory()
    await validator.validate(f"Bearer {valid}")
    claims = jwt.decode(valid, options={"verify_signature": False})
    for index in range(5):
        token = jwt.encode(
            claims, signing_key, algorithm="RS256", headers={"kid": f"absent-{index}"}
        )
        with pytest.raises(ApiError):
            await validator.validate(f"Bearer {token}")
    assert len(harness.requests) == 2


async def test_rotated_key_refresh(harness, token_factory, signing_key):
    validator = harness.services.validator
    await validator.validate(f"Bearer {token_factory()}")
    validator._last_refresh -= 31
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    public["kid"] = "rotated"
    original = harness.client._transport.handler

    def rotated(request):
        if request.url.path.endswith("/keys"):
            return httpx.Response(200, json={"keys": [public]})
        return original(request)

    harness.client._transport.handler = rotated
    claims = jwt.decode(token_factory(), options={"verify_signature": False})
    token = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "rotated"})
    assert await validator.validate(f"Bearer {token}")


async def test_identity_outage_refresh_is_throttled(settings, token_factory):
    requests = []

    def unavailable(request):
        requests.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        validator = TokenValidator(settings, client)
        for _ in range(3):
            with pytest.raises(ApiError) as failure:
                await validator.validate(f"Bearer {token_factory()}")
            assert failure.value.status == 503
    assert len(requests) == 1


@pytest.mark.parametrize("metadata", [[], {"issuer": "wrong"}, {"jwks_uri": 123}])
async def test_invalid_identity_metadata_fails_closed(settings, token_factory, metadata):
    if isinstance(metadata, dict) and "issuer" not in metadata:
        metadata["issuer"] = settings.issuer
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=metadata))
    ) as client:
        with pytest.raises(ApiError) as failure:
            await TokenValidator(settings, client).validate(f"Bearer {token_factory()}")
    assert failure.value.status == 503
