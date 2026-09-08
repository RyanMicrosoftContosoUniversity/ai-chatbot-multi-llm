from typing import Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", enable_decoding=False)

    entra_tenant_id: UUID
    entra_api_client_id: UUID
    entra_required_scope: str = "chat.access"
    apim_endpoint: str
    apim_subscription_key: SecretStr | None = None
    key_vault_url: str | None = None
    apim_secret_name: str = "apim-subscription-key"
    apim_subscription_header: str = "Ocp-Apim-Subscription-Key"
    allowed_models: list[str] = Field(default_factory=lambda: ["gpt-4o", "luna", "deepseek"])
    stream_usage_models: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    max_tokens_cap: int = Field(default=2000, ge=1, le=16384)
    max_message_chars: int = Field(default=12000, ge=1, le=100000)
    max_context_chars: int = Field(default=24000, ge=1, le=200000)
    max_body_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_active_generations: int = Field(default=8, ge=1, le=100)
    connect_timeout_seconds: float = Field(default=10, gt=0, le=60)
    read_timeout_seconds: float = Field(default=90, gt=0, le=300)
    generation_timeout_seconds: float = Field(default=180, gt=0, le=600)
    heartbeat_seconds: float = Field(default=10, gt=0, le=30)
    cosmos_endpoint: str | None = None
    cosmos_database: str = "chatdb"
    cosmos_conversations_container: str = "conversations"
    cosmos_messages_container: str = "messages"
    applicationinsights_connection_string: SecretStr | None = None
    managed_identity_client_id: str | None = None

    @field_validator("allowed_models", "stream_usage_models", "cors_origins", mode="before")
    @classmethod
    def comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator(
        "key_vault_url",
        "cosmos_endpoint",
        "applicationinsights_connection_string",
        "managed_identity_client_id",
        mode="before",
    )
    @classmethod
    def blank_optional_setting(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("cors_origins")
    @classmethod
    def exact_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            parsed = urlsplit(origin)
            local = parsed.hostname in {"localhost", "127.0.0.1"}
            if (
                "*" in origin
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or not parsed.hostname
                or (parsed.scheme != "https" and not (local and parsed.scheme == "http"))
            ):
                raise ValueError(
                    "CORS origins must be exact HTTPS origins (HTTP localhost allowed)"
                )
        return origins

    @field_validator("apim_endpoint", "key_vault_url", "cosmos_endpoint")
    @classmethod
    def https_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Azure endpoints must be HTTPS URLs without credentials or query strings"
            )
        return value.rstrip("/")

    @field_validator("allowed_models")
    @classmethod
    def nonempty_models(cls, models: list[str]) -> list[str]:
        if not models or any(not item or len(item) > 100 for item in models):
            raise ValueError("At least one valid model alias is required")
        return list(dict.fromkeys(models))

    @model_validator(mode="after")
    def secret_source(self) -> Self:
        if not set(self.stream_usage_models).issubset(self.allowed_models):
            raise ValueError("STREAM_USAGE_MODELS must be a subset of ALLOWED_MODELS")
        if self.apim_subscription_key is not None:
            if not self.apim_subscription_key.get_secret_value().strip():
                raise ValueError("APIM_SUBSCRIPTION_KEY must not be empty")
        elif not self.key_vault_url:
            raise ValueError("Configure a Key Vault reference key or KEY_VAULT_URL")
        return self

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/v2.0"
