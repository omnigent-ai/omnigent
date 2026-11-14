"""Tests for the Claude subscription connection routes and their broker vend.

Builds a minimal FastAPI app with the Claude router, the host credential broker,
and a header-based auth provider, so the save → status → vend → disconnect flow
runs without KMS or a sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from omnigent.connections.claude import ClaudeConnectionStore
from omnigent.errors import OmnigentError
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.server.claude_connection import (
    CLAUDE_CONNECT_ENV_VAR,
    ClaudeConnectConfig,
    InvalidSetupTokenError,
    normalize_setup_token,
)
from omnigent.server.routes.connections_claude import create_connections_claude_router
from omnigent.server.routes.host_credentials import create_host_credentials_router


class SecretBox:  # test double for the KMS SecretCipher: key- and context-bound
    def __init__(self, key: str) -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context) -> str:
        import base64
        import json

        return base64.b64encode(
            json.dumps({"k": self._key, "c": dict(context), "p": plaintext}).encode()
        ).decode("ascii")

    def decrypt(self, ciphertext: str, *, context):
        import base64
        import json

        try:
            d = json.loads(base64.b64decode(ciphertext.encode("ascii")))
        except ValueError:
            return None
        return d["p"] if d["k"] == self._key and d["c"] == dict(context) else None


class _HeaderAuth:
    """Auth provider reading the user id from ``X-Test-User``."""

    def get_user_id(self, request: object) -> str | None:
        return getattr(request, "headers", {}).get("x-test-user")


@dataclass
class _Managed:
    user_id: str


class _FakeHostStore:
    """Resolves each launch token to the owner of the host it was issued for."""

    def __init__(self, hosts: dict[tuple[str, str], str]) -> None:
        self._hosts = hosts

    def resolve_launch_token(self, host_id: str, token: str) -> _Managed | None:
        owner = self._hosts.get((host_id, token))
        return _Managed(owner) if owner is not None else None


_ALICE = {"X-Test-User": "alice@example.com"}
_TOKEN = "sk-ant-oat01-alice-subscription"


def _app(db_uri: str) -> tuple[TestClient, ClaudeConnectionStore]:
    store = ClaudeConnectionStore(db_uri, SecretBox("store-enc-key"))
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(status_code=exc.http_status, content={"error": exc.message})

    app.state.claude_store = store
    app.state.claude_client = None
    app.include_router(
        create_connections_claude_router(
            ClaudeConnectConfig(), store, auth_provider=_HeaderAuth()
        ),
        prefix="/v1",
    )
    hosts = _FakeHostStore(
        {
            ("alice-host", "alice-launch"): "alice@example.com",
            ("bob-host", "bob-launch"): "bob@example.com",
        }
    )
    app.include_router(create_host_credentials_router(hosts), prefix="/v1")  # type: ignore[arg-type]
    return TestClient(app), store


def test_saved_token_is_vended_only_to_the_owners_sandbox(db_uri: str) -> None:
    client, store = _app(db_uri)
    assert client.get("/v1/connections/claude/status", headers=_ALICE).json() == {
        "enabled": True,
        "connected": False,
        "connected_at": None,
    }

    saved = client.put(
        "/v1/connections/claude/token", headers=_ALICE, json={"token": f"  {_TOKEN}\n"}
    )
    assert saved.status_code == 200
    assert saved.json()["connected"] is True
    assert _TOKEN not in saved.text
    status = client.get("/v1/connections/claude/status", headers=_ALICE).json()
    assert status["connected"] is True and isinstance(status["connected_at"], int)
    assert _TOKEN not in str(status)
    stored = store.get("alice@example.com", with_tokens=True)
    assert stored is not None and stored.oauth_token == _TOKEN

    vended = client.get(
        "/v1/hosts/alice-host/credentials/claude",
        headers={MANAGED_HOST_TOKEN_HEADER: "alice-launch"},
    )
    assert vended.headers.get("cache-control") == "no-store"
    assert vended.json() == {"connected": True, "owner": "alice@example.com", "token": _TOKEN}
    other_owner = client.get(
        "/v1/hosts/bob-host/credentials/claude",
        headers={MANAGED_HOST_TOKEN_HEADER: "bob-launch"},
    )
    assert other_owner.json() == {"connected": False}

    assert client.post("/v1/connections/claude/disconnect", headers=_ALICE).json() == {
        "disconnected": True
    }
    after = client.get(
        "/v1/hosts/alice-host/credentials/claude",
        headers={MANAGED_HOST_TOKEN_HEADER: "alice-launch"},
    )
    assert after.json() == {"connected": False}


def test_saving_again_replaces_the_token(db_uri: str) -> None:
    client, store = _app(db_uri)
    client.put("/v1/connections/claude/token", headers=_ALICE, json={"token": _TOKEN})
    replacement = "sk-ant-oat01-replacement"
    client.put("/v1/connections/claude/token", headers=_ALICE, json={"token": replacement})
    stored = store.get("alice@example.com", with_tokens=True)
    assert stored is not None and stored.oauth_token == replacement


def test_routes_require_a_signed_in_user(db_uri: str) -> None:
    client, _store = _app(db_uri)
    assert client.get("/v1/connections/claude/status").status_code == 401
    assert client.put("/v1/connections/claude/token", json={"token": _TOKEN}).status_code == 401
    assert client.post("/v1/connections/claude/disconnect").status_code == 401


@pytest.mark.parametrize(
    "token",
    [
        "sk-ant-api03-an-api-key",
        "not-a-token",
        "",
        "sk-ant-oat01-has space",
        "sk-ant-oat" + "x" * 1100,
    ],
)
def test_rejects_values_that_are_not_setup_tokens(db_uri: str, token: str) -> None:
    client, store = _app(db_uri)
    response = client.put("/v1/connections/claude/token", headers=_ALICE, json={"token": token})
    assert response.status_code == 422
    assert store.get("alice@example.com") is None
    with pytest.raises(InvalidSetupTokenError):
        normalize_setup_token(token)


@pytest.mark.parametrize(
    ("value", "enabled"), [(None, False), ("0", False), ("1", True), ("true", True)]
)
def test_config_is_opt_in(
    monkeypatch: pytest.MonkeyPatch, value: str | None, enabled: bool
) -> None:
    if value is None:
        monkeypatch.delenv(CLAUDE_CONNECT_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(CLAUDE_CONNECT_ENV_VAR, value)
    assert (ClaudeConnectConfig.from_env() is not None) is enabled
