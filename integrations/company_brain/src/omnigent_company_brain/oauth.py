from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnigent_company_brain.oauth_config import (
    CLIENT_CREDENTIAL_FORM_FIELD,
    PROVIDERS,
    ProviderName,
    ResolvedOAuthClient,
)


class OAuthState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderName
    workspace_id: int
    admin_id: str = Field(min_length=1, max_length=128)
    redirect_uri: str = Field(min_length=1, max_length=2048)
    issued_at_ms: int
    nonce: str = Field(min_length=16, max_length=256)
    return_to: str | None = Field(default=None, max_length=2048)
    code_verifier: str | None = Field(default=None, min_length=43, max_length=128)

    @field_validator("redirect_uri")
    @classmethod
    def _validate_redirect_uri(cls, value: str) -> str:
        if not value.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
            raise ValueError("OAuth redirect URI must use HTTPS or loopback HTTP")
        return value

    @field_validator("return_to")
    @classmethod
    def _validate_return_to(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/") or value.startswith("//") or "\\" in value:
            raise ValueError("OAuth return path must be a same-origin absolute path")
        return value


class OAuthToken(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    access_token: str = Field(min_length=1, repr=False)
    refresh_token: str | None = Field(default=None, repr=False)
    expires_at_ms: int | None = None
    granted_scopes: tuple[str, ...] = ()
    account_label: str | None = None
    provider_metadata: dict[str, str] = Field(default_factory=dict)


class OAuthStateCodec:
    def __init__(self, secret: str, *, max_age_ms: int = 10 * 60_000) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("OAuth state secret must be at least 32 bytes")
        self._secret = secret
        self._max_age_ms = max_age_ms

    def seal(
        self,
        *,
        provider: ProviderName,
        workspace_id: int,
        admin_id: str,
        redirect_uri: str,
        return_to: str | None = None,
        now_ms: int | None = None,
    ) -> tuple[str, OAuthState]:
        config = PROVIDERS[provider]
        state = OAuthState(
            provider=provider,
            workspace_id=workspace_id,
            admin_id=admin_id,
            redirect_uri=redirect_uri,
            issued_at_ms=now_ms if now_ms is not None else int(time.time() * 1000),
            nonce=secrets.token_urlsafe(32),
            return_to=return_to,
            code_verifier=generate_code_verifier() if config.pkce else None,
        )
        token = jwt.encode(state.model_dump(), self._secret, algorithm="HS256")
        return token, state

    def open(
        self,
        token: str,
        *,
        expected_provider: ProviderName,
        now_ms: int | None = None,
    ) -> OAuthState:
        decoded = jwt.decode(token, self._secret, algorithms=["HS256"])
        state = OAuthState.model_validate(decoded)
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        if state.provider != expected_provider:
            raise ValueError("OAuth state provider mismatch")
        if state.issued_at_ms > now + 60_000 or now - state.issued_at_ms > self._max_age_ms:
            raise ValueError("OAuth state expired")
        return state


def nonce_digest(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(48)[:64]


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def authorize_url(
    provider: ProviderName,
    *,
    client: ResolvedOAuthClient,
    redirect_uri: str,
    sealed_state: str,
    code_verifier: str | None,
) -> str:
    config = PROVIDERS[provider]
    parameters = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": sealed_state,
        config.scope_parameter: " ".join(config.scopes),
        **config.auth_parameters,
    }
    if code_verifier:
        parameters.update(
            {"code_challenge": code_challenge_s256(code_verifier), "code_challenge_method": "S256"}
        )
    return f"{config.auth_url}?{urlencode(parameters)}"


def _parse_scopes(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part for part in value.replace(",", " ").split() if part)
    if isinstance(value, list):
        return tuple(str(part) for part in value)
    return ()


def _token_from_payload(payload: dict[str, Any], *, now_ms: int) -> OAuthToken:
    expires_in = payload.get("expires_in")
    return OAuthToken(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload["refresh_token"]) if payload.get("refresh_token") else None,
        expires_at_ms=(now_ms + int(expires_in) * 1000) if expires_in else None,
        granted_scopes=_parse_scopes(payload.get("scope")),
    )


async def exchange_code(
    provider: ProviderName,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str | None,
    client_config: ResolvedOAuthClient,
    http_client: httpx.AsyncClient,
    now_ms: int | None = None,
) -> OAuthToken:
    config = PROVIDERS[provider]
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if provider == "notion":
        response = await http_client.post(
            config.token_url,
            auth=(client_config.client_id, client_config.client_credential),
            headers={"Notion-Version": "2022-06-28"},
            json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        )
    else:
        response = await http_client.post(
            config.token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_config.client_id,
                CLIENT_CREDENTIAL_FORM_FIELD: client_config.client_credential,
                **({"code_verifier": code_verifier} if code_verifier else {}),
            },
        )
    if not response.is_success:
        raise RuntimeError(f"{provider} token exchange failed ({response.status_code})")
    payload = response.json()
    if provider == "slack":
        if payload.get("ok") is not True:
            raise RuntimeError("Slack rejected the OAuth exchange")
        token = _token_from_payload(payload, now_ms=now)
        return token.model_copy(
            update={
                "account_label": (payload.get("team") or {}).get("name"),
                "provider_metadata": {
                    "team_id": str((payload.get("team") or {}).get("id") or ""),
                },
            }
        )
    token = _token_from_payload(payload, now_ms=now)
    if provider == "notion":
        return token.model_copy(
            update={
                "account_label": str(payload.get("workspace_name") or "Notion workspace"),
                "provider_metadata": {
                    "workspace_id": str(payload.get("workspace_id") or ""),
                },
            }
        )
    if client_config.hosted_domain:
        id_token = str(payload.get("id_token") or "")
        claims = jwt.decode(id_token, options={"verify_signature": False}) if id_token else {}
        if str(claims.get("hd") or "").lower() != client_config.hosted_domain.lower():
            raise RuntimeError("connected Google account is outside the configured workspace")
    return token.model_copy(
        update={"account_label": str(payload.get("email") or "Google Workspace")}
    )


async def refresh_google_token(
    token: OAuthToken,
    *,
    client_config: ResolvedOAuthClient,
    http_client: httpx.AsyncClient,
    now_ms: int | None = None,
) -> OAuthToken:
    if not token.refresh_token:
        raise ValueError("Google connection has no refresh token")
    response = await http_client.post(
        PROVIDERS["google"].token_url,
        data={
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": client_config.client_id,
            CLIENT_CREDENTIAL_FORM_FIELD: client_config.client_credential,
        },
    )
    if not response.is_success:
        raise RuntimeError(f"Google token refresh failed ({response.status_code})")
    refreshed = _token_from_payload(
        {**response.json(), "refresh_token": token.refresh_token},
        now_ms=now_ms if now_ms is not None else int(time.time() * 1000),
    )
    return refreshed.model_copy(
        update={
            "account_label": token.account_label,
            "granted_scopes": token.granted_scopes,
            "provider_metadata": token.provider_metadata,
        }
    )
