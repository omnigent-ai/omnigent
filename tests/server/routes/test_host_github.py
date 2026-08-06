"""Tests for the host-facing GitHub-credential endpoint."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.connections.github import GithubConnectionStore
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.server.github_app import GitHubTokenSet
from omnigent.server.routes.host_github import create_host_github_router


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


@dataclass
class _Managed:
    user_id: str


class _FakeHostStore:
    """Resolves a single (host_id, token) pair to an owner."""

    def __init__(self, host_id: str, token: str, owner: str) -> None:
        self._host_id, self._token, self._owner = host_id, token, owner

    def resolve_launch_token(self, host_id: str, token: str) -> _Managed | None:
        if host_id == self._host_id and token == self._token:
            return _Managed(self._owner)
        return None


def _app(db_uri: str, host_store: _FakeHostStore) -> tuple[TestClient, GithubConnectionStore]:
    store = GithubConnectionStore(db_uri, SecretBox("enc-secret"))
    app = FastAPI()
    app.include_router(
        create_host_github_router(host_store, store, None),  # type: ignore[arg-type]
        prefix="/v1",
    )
    return TestClient(app), store


_HDR = {MANAGED_HOST_TOKEN_HEADER: "launch-tok"}


def test_returns_token_for_valid_host_token(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    tc, store = _app(db_uri, hs)
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_live", "ghr_x", None, None, "repo"),
    )
    resp = tc.get("/v1/hosts/host1/github-credential", headers=_HDR)
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.json() == {
        "connected": True,
        "username": "x-access-token",
        "token": "ghu_live",
        "owner": "alice@example.com",
        "login": "octocat",
    }


def test_unauthenticated_without_or_with_bad_token(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    tc, _store = _app(db_uri, hs)
    assert tc.get("/v1/hosts/host1/github-credential").status_code == 401
    bad = tc.get("/v1/hosts/host1/github-credential", headers={MANAGED_HOST_TOKEN_HEADER: "nope"})
    assert bad.status_code == 401
    # Right token but wrong host id → also fails closed.
    other = tc.get("/v1/hosts/other/github-credential", headers=_HDR)
    assert other.status_code == 401


def test_connected_false_when_owner_has_no_github(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    tc, _store = _app(db_uri, hs)  # no upsert → owner hasn't linked GitHub
    resp = tc.get("/v1/hosts/host1/github-credential", headers=_HDR)
    assert resp.status_code == 200
    assert resp.json() == {"connected": False}
