import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import ApiError
from app.services.concurrency import Capacity


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "https://*.example.com",
        "http://remote.example",
        "https://example.com/path",
        "https://user:pass@example.com",
    ],
)
def test_origins_are_exact(settings, origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**settings.model_dump(), "cors_origins": [origin]})


def test_fail_closed_without_required_identity():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, entra_tenant_id="", entra_api_client_id="")


def test_fail_closed_without_secret_source(settings):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            **{
                **settings.model_dump(),
                "apim_subscription_key": None,
                "key_vault_url": None,
            },
        )


async def test_per_user_and_total_capacity_release():
    guard = Capacity(1)
    async with guard.acquire("user"):
        with pytest.raises(ApiError) as duplicate:
            async with guard.acquire("user"):
                pass
        assert duplicate.value.status == 409
        with pytest.raises(ApiError) as total:
            async with guard.acquire("other"):
                pass
        assert total.value.status == 429
    async with guard.acquire("other"):
        pass


async def test_capacity_released_on_failure():
    guard = Capacity(1)
    with pytest.raises(ValueError):
        async with guard.acquire("user"):
            raise ValueError("test")
    async with guard.acquire("user"):
        pass


def test_optional_empty_settings_disable_optional_services(settings):
    values = settings.model_dump()
    values.update(cosmos_endpoint="", applicationinsights_connection_string="")
    configured = Settings(_env_file=None, **values)
    assert configured.cosmos_endpoint is None
    assert configured.applicationinsights_connection_string is None


def test_usage_models_must_be_allowed(settings):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**settings.model_dump(), "stream_usage_models": ["unknown"]})
