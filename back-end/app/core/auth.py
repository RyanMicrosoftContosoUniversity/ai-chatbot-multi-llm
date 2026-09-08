import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt

from .config import Settings
from .errors import ApiError


@dataclass(frozen=True)
class User:
    oid: str
    tenant_id: str


class TokenValidator:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires = 0.0
        self._last_refresh = float("-inf")
        self._refresh_failed = False
        self._lock = asyncio.Lock()

    async def _key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            now = time.monotonic()
            expired = now >= self._expires
            if expired and self._refresh_failed and now - self._last_refresh < 30:
                raise ApiError(
                    503, "identity_unavailable", "Identity verification is temporarily unavailable."
                )
            if expired or (kid not in self._keys and now - self._last_refresh >= 30):
                self._last_refresh = now
                try:
                    discovery = await self.client.get(
                        f"{self.settings.issuer}/.well-known/openid-configuration"
                    )
                    discovery.raise_for_status()
                    metadata = discovery.json()
                    if not isinstance(metadata, dict):
                        raise ValueError("Invalid discovery metadata")
                    if metadata.get("issuer") != self.settings.issuer:
                        raise ValueError("Unexpected issuer")
                    url = metadata["jwks_uri"]
                    if not isinstance(url, str):
                        raise ValueError("Invalid signing key endpoint")
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme != "https"
                        or parsed.hostname != "login.microsoftonline.com"
                        or parsed.username
                        or parsed.password
                        or parsed.port not in (None, 443)
                    ):
                        raise ValueError("Untrusted signing key endpoint")
                    response = await self.client.get(url)
                    response.raise_for_status()
                    document = response.json()
                    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
                        raise ValueError("Invalid signing key metadata")
                    keys = document["keys"]
                    self._keys = {
                        item["kid"]: jwt.PyJWK.from_dict(item, algorithm="RS256")
                        for item in keys
                        if isinstance(item, dict)
                        and item.get("kty") == "RSA"
                        and item.get("use", "sig") == "sig"
                        and item.get("alg", "RS256") == "RS256"
                    }
                    if not self._keys:
                        raise ValueError("No signing keys")
                    self._expires = now + 3600
                    self._refresh_failed = False
                except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError) as exc:
                    self._refresh_failed = True
                    raise ApiError(
                        503,
                        "identity_unavailable",
                        "Identity verification is temporarily unavailable.",
                    ) from exc
            if kid not in self._keys:
                raise ApiError(401, "invalid_token", "The access token is not valid for this API.")
            return self._keys[kid]

    async def validate(self, authorization: str | None) -> User:
        if not authorization or len(authorization) > 16384:
            raise ApiError(401, "authentication_required", "An API access token is required.")
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token:
            raise ApiError(401, "authentication_required", "An API access token is required.")
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or len(kid) > 200:
                raise jwt.InvalidTokenError()
            key = await self._key(kid)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=str(self.settings.entra_api_client_id),
                issuer=self.settings.issuer,
                leeway=30,
                options={
                    "require": ["exp", "nbf", "iat", "iss", "aud", "tid", "oid", "ver"],
                    "strict_aud": True,
                },
            )
            if claims["tid"] != str(self.settings.entra_tenant_id) or claims["ver"] != "2.0":
                raise jwt.InvalidTokenError()
            oid = str(UUID(claims["oid"]))
        except (jwt.PyJWTError, ValueError, TypeError, AttributeError) as exc:
            raise ApiError(
                401, "invalid_token", "The access token is not valid for this API."
            ) from exc
        scopes = claims.get("scp")
        if not isinstance(scopes, str) or self.settings.entra_required_scope not in scopes.split():
            raise ApiError(403, "permission_denied", "The delegated chat permission is required.")
        return User(oid, claims["tid"])
